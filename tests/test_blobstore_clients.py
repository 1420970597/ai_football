"""Media connections reuse pools without caching objects or credentials across changes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
from unittest.mock import Mock, patch

from app import blobstore


class BlobstoreClientTests(unittest.TestCase):
    def setUp(self):
        blobstore.close_clients()
        self.environment = patch.dict(os.environ, {
            'S3_ENDPOINT_URL': 'https://media.invalid', 'S3_ACCESS_KEY': 'isolated-access',
            'S3_SECRET_KEY': 'isolated-secret', 'S3_BUCKET': 'isolated-media',
            'S3_REGION': 'us-east-1', 'S3_USE_TLS': 'true', 'S3_HTTPS_PROXY': ''})
        self.environment.start()
        self.created = []

        def create(*args, **kwargs):
            client = Mock()
            self.created.append(client)
            return client
        self.factory = Mock(side_effect=create)
        self.session_patch = patch.object(blobstore.boto3.session, 'Session',
                                          side_effect=lambda: Mock(client=self.factory))
        self.session_patch.start()

    def tearDown(self):
        blobstore.close_clients()
        self.session_patch.stop()
        self.environment.stop()

    def test_same_thread_reuses_client_and_preserves_network_budgets(self):
        first = blobstore._s3_client()
        self.assertIs(first, blobstore._s3_client())
        self.factory.assert_called_once()
        options = self.factory.call_args.kwargs
        self.assertTrue(options['use_ssl'])
        self.assertEqual(3, options['config'].connect_timeout)
        self.assertEqual(8, options['config'].read_timeout)
        self.assertEqual({'max_attempts': 2}, options['config'].retries)

    def test_each_thread_owns_its_session_and_client(self):
        barrier = threading.Barrier(2)
        def read():
            first = blobstore._s3_client()
            barrier.wait(timeout=5)
            return first, blobstore._s3_client()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(read) for _ in range(2)]
            results = [f.result(timeout=5) for f in futures]
        self.assertIsNot(results[0][0], results[1][0])
        for first, second in results:
            self.assertIs(first, second)
        self.assertEqual(2, self.factory.call_count)
        blobstore.close_clients()
        for client in self.created:
            client.close.assert_called_once()

    def test_configuration_changes_retire_previous_pool(self):
        for name, value in [('S3_ENDPOINT_URL', 'https://other.invalid'),
                            ('S3_ACCESS_KEY', 'rotated-access'), ('S3_SECRET_KEY', 'rotated-secret'),
                            ('S3_REGION', 'eu-west-1'), ('S3_USE_TLS', 'false'),
                            ('S3_HTTPS_PROXY', 'http://proxy.invalid:8080'),
                            ('NO_PROXY', 'media.invalid'), ('AWS_CA_BUNDLE', '/isolated/ca.pem')]:
            with self.subTest(name=name):
                old = blobstore._s3_client()
                os.environ[name] = value
                new = blobstore._s3_client()
                self.assertIsNot(old, new)
                old.close.assert_called_once()
                self.assertIs(new, blobstore._s3_client())

    def test_missing_configuration_does_not_reuse_old_authorization(self):
        first = blobstore._s3_client()
        del os.environ['S3_SECRET_KEY']
        with self.assertRaisesRegex(RuntimeError, 'not configured'):
            blobstore._s3_client()
        first.close.assert_called_once()
        self.assertEqual(1, self.factory.call_count)

    def test_failed_creation_cannot_fall_back_to_old_client(self):
        first = blobstore._s3_client()
        os.environ['S3_SECRET_KEY'] = 'replacement'
        self.factory.side_effect = RuntimeError('isolated creation failed')
        with self.assertRaisesRegex(RuntimeError, 'isolated creation failed'):
            blobstore._s3_client()
        first.close.assert_called_once()
        self.assertEqual({}, blobstore._clients)

    def test_invalid_object_paths_do_not_open_a_client(self):
        for key in ('', '/private/image.png', 'private/../image.png'):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'invalid remote media'):
                blobstore._read_object(key)
        self.factory.assert_not_called()

    def test_shutdown_is_idempotent_and_next_read_creates_fresh_pool(self):
        first = blobstore._s3_client()
        blobstore.close_clients()
        blobstore.close_clients()
        first.close.assert_called_once()
        self.assertIsNot(first, blobstore._s3_client())

    def test_shutdown_closes_all_clients_after_one_close_failure(self):
        first = blobstore._s3_client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(blobstore._s3_client).result(timeout=5)
        first.close.side_effect = RuntimeError('secret must not be logged')
        with self.assertLogs(blobstore.__name__, level='WARNING') as captured:
            blobstore.close_clients()
        self.assertNotIn('secret must not be logged', ''.join(captured.output))
        second.close.assert_called_once()
        self.assertEqual({}, blobstore._clients)

    def test_every_object_gets_a_fresh_response_and_body_is_closed(self):
        client = blobstore._s3_client()
        bodies = [Mock(), Mock()]
        for body in bodies:
            body.read.return_value = b'object bytes'
        client.get_object.side_effect = [{'Body': body} for body in bodies]
        for _ in bodies:
            self.assertEqual(b'object bytes', blobstore._read_object('private/image.png'))
        self.assertEqual(1, self.factory.call_count)
        self.assertEqual(2, client.get_object.call_count)
        for body in bodies:
            body.close.assert_called_once()

    def test_read_failure_and_excess_size_close_stream(self):
        client = blobstore._s3_client()
        for failure in (False, True):
            with self.subTest(read_failure=failure):
                body = Mock()
                body.read.side_effect = OSError('isolated read error') if failure else None
                body.read.return_value = b'oversized'
                client.get_object.return_value = {'Body': body}
                with patch.object(blobstore, 'MAX_IMAGE_BYTES', 2):
                    with self.assertRaises(OSError if failure else RuntimeError):
                        blobstore._read_object('private/image.png')
                body.close.assert_called_once()

    def test_async_read_and_validation_stay_off_event_loop(self):
        main_thread = threading.get_ident()
        calls = []
        def read(key):
            calls.append(('read', threading.get_ident()))
            return b'bytes'
        def validate(data):
            calls.append(('validate', threading.get_ident()))
        with patch.object(blobstore, '_read_object', side_effect=read), \
                patch.object(blobstore, 'validate_image', side_effect=validate):
            self.assertEqual(b'bytes', asyncio.run(blobstore.read_image('private/image.png')))
        self.assertEqual(['read', 'validate'], [kind for kind, _ in calls])
        self.assertTrue(all(thread != main_thread for _, thread in calls))
