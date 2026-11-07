import json
from pathlib import Path
import socket
import threading
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from infra.config.reuse_inventory import parse_env, private_json, render_compose_env, summarize
from infra.tunnel.gateway import Config, parse_connect, ssh_command
from infra.tunnel.gateway import ConnectServer
from infra.tunnel import gateway
from infra.config.check_compose import PERMITTED, validate as validate_compose


class ConfigurationReuseTests(unittest.TestCase):
    def test_env_keeps_quoted_dollar_and_password_characters(self):
        self.assertEqual({"TOKEN": "value$with spaces#x", "EMPTY": ""}, parse_env("TOKEN='value$with spaces#x'\nEMPTY=\n"))

    def test_invalid_env_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_env("not a key=value")
        with self.assertRaises(ValueError):
            parse_env("TOKEN=abc\0def")
        with self.assertRaises(ValueError):
            parse_env("TOKEN='unclosed")
        with self.assertRaises(ValueError):
            parse_env("TOKEN=a\nTOKEN=b")

    def test_literal_env_roundtrip_and_multiline_rejection(self):
        expected = {"TOKEN": "literal$token'with\\backslash\"", "EMPTY": ""}
        self.assertEqual(expected, parse_env(render_compose_env(expected)))
        for invalid in ("first\nsecond", "first\rsecond", "first\0second"):
            with self.assertRaises(ValueError):
                render_compose_env({"TOKEN": invalid})

    def test_private_file_permissions_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            private_json(path, {"test": "example"})
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            link = root / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                private_json(link, {})

    def test_summary_does_not_include_values(self):
        value = {"password": "do-not-return-me", "sources": [{"secret": "secretvalue"}]}
        serialized = json.dumps(summarize(value))
        self.assertNotIn("do-not-return-me", serialized)
        self.assertNotIn("secretvalue", serialized)


class TunnelBoundaryTests(unittest.TestCase):
    def test_only_exact_connect_target_allowed(self):
        parse_connect(b"CONNECT storage.invalid:9000 HTTP/1.1\r\nHost: storage.invalid:9000\r\n\r\n", "storage.invalid:9000")
        for request in [b"CONNECT other.invalid:9000 HTTP/1.1\r\n\r\n", b"GET / HTTP/1.1\r\n\r\n", b"CONNECT storage.invalid:9000 HTTP/1.1\r\nHost: other.invalid:9000\r\n\r\n", b"CONNECT storage.invalid:9000 HTTP/1.1\r\nContent-Length: 10\r\n\r\n"]:
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    parse_connect(request, "storage.invalid:9000")

    def test_ssh_strict_host_keys_and_only_fixed_forwards(self):
        config = Config("/run/storage/ssh", "storage-vps", "127.0.0.1", 5432, 15432, "127.0.0.1", 9000, 19000, 18080, "storage.invalid:9000")
        command = ssh_command(config)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertEqual(2, command.count("-L"))
        self.assertNotIn("-D", command)
        self.assertNotIn("-R", command)

    def test_separate_transports_preserve_host_checks_and_cannot_multiplex(self):
        config = Config('/run/storage/ssh', 'storage-vps', '127.0.0.1', 5432, 15432,
                        '127.0.0.1', 9000, 19000, 18080, 'storage.invalid:9000')
        for mode, forward in [('database', '0.0.0.0:15432:127.0.0.1:5432'),
                              ('media', '127.0.0.1:19000:127.0.0.1:9000')]:
            with self.subTest(mode=mode):
                command = ssh_command(config, transport=mode)
                self.assertEqual(1, command.count('-L'))
                self.assertEqual(forward, command[command.index('-L')+1])
                for option in ['StrictHostKeyChecking=yes', 'ExitOnForwardFailure=yes',
                               'ControlMaster=no', 'ControlPath=none', 'ConnectTimeout=10']:
                    self.assertIn(option, command)
                self.assertEqual('storage-vps', command[-1])
                self.assertFalse({'-D', '-R'} & set(command))
        with self.assertRaises(ValueError):
            ssh_command(config, transport='unreviewed')

    def test_transport_failure_closes_proxy_and_reaps_both_children(self):
        config = Config('/run/storage/ssh', 'storage-vps', '127.0.0.1', 5432, 15432,
                        '127.0.0.1', 9000, 19000, 0, 'storage.invalid:9000')
        for failed in (0, 1):
            with self.subTest(failed=failed):
                children = [Mock(), Mock()]
                for index, child in enumerate(children):
                    child.poll.return_value = 1 if index == failed else None
                addresses = []
                class ObservedServer(ConnectServer):
                    def __enter__(self):
                        addresses.append(self.server_address)
                        return super().__enter__()
                with patch.object(gateway.subprocess, 'Popen', side_effect=children) as spawn, \
                     patch.object(gateway.signal, 'signal'), patch.object(gateway, 'ConnectServer', ObservedServer):
                    self.assertEqual(1, gateway.run(config))
                self.assertEqual(2, spawn.call_count)
                children[1-failed].terminate.assert_called_once()
                for child in children:
                    child.wait.assert_called_once_with(timeout=5)
                with self.assertRaises(OSError):
                    socket.create_connection(('127.0.0.1', addresses[0][1]), timeout=1)

    def test_partial_transport_startup_failure_reaps_started_child(self):
        config = Config('/run/storage/ssh', 'storage-vps', '127.0.0.1', 5432, 15432,
                        '127.0.0.1', 9000, 19000, 0, 'storage.invalid:9000')
        child = Mock()
        child.poll.return_value = None
        with patch.object(gateway.subprocess, 'Popen', side_effect=[child, OSError('isolated-start-failure')]), \
             patch.object(gateway.signal, 'signal'):
            with self.assertRaises(OSError):
                gateway.run(config)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=5)

    def test_shutdown_signal_reaps_real_transport_children_and_closes_proxy(self):
        config = Config('/run/storage/ssh', 'storage-vps', '127.0.0.1', 5432, 15432,
                        '127.0.0.1', 9000, 19000, 0, 'storage.invalid:9000')
        real_spawn = gateway.subprocess.Popen
        children, results, handlers, addresses = [], [], {}, []
        ready = threading.Event()
        class ObservedServer(ConnectServer):
            def __enter__(self):
                addresses.append(self.server_address)
                ready.set()
                return super().__enter__()
        def spawn(command, **kwargs):
            self.assertEqual('ssh', command[0])
            child = real_spawn([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        def register(kind, handler):
            handlers[kind] = handler
        worker = threading.Thread(target=lambda: results.append(gateway.run(config)), daemon=True)
        with patch.object(gateway.subprocess, 'Popen', side_effect=spawn), \
             patch.object(gateway.signal, 'signal', side_effect=register), \
             patch.object(gateway, 'ConnectServer', ObservedServer):
            try:
                worker.start()
                self.assertTrue(ready.wait(5), 'proxy and both child processes must start')
                self.assertEqual(2, len(children))
                handlers[gateway.signal.SIGTERM](gateway.signal.SIGTERM, None)
                worker.join(6)
                self.assertFalse(worker.is_alive())
                self.assertEqual([0], results)
                self.assertTrue(all(child.poll() is not None for child in children))
                with self.assertRaises(OSError):
                    socket.create_connection(('127.0.0.1', addresses[0][1]), timeout=1)
            finally:
                if gateway.signal.SIGTERM in handlers:
                    handlers[gateway.signal.SIGTERM](gateway.signal.SIGTERM, None)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                worker.join(3)

    def test_connect_relays_bytes_and_rejects_other_authorities(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def echo():
            with listener:
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(connection.recv(32))

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        with ConnectServer(("127.0.0.1", 0), "allowed.invalid:9000", listener.getsockname()[1]) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with socket.create_connection(server.server_address, timeout=3) as client:
                    client.sendall(b"CONNECT allowed.invalid:9000 HTTP/1.1\r\n\r\n")
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        header += client.recv(1)
                    self.assertIn(b"200 Connection Established", header)
                    client.sendall(b"bounded-test")
                    self.assertEqual(b"bounded-test", client.recv(32))
                with socket.create_connection(server.server_address, timeout=3) as client:
                    client.sendall(b"CONNECT other.invalid:9000 HTTP/1.1\r\n\r\n")
                    self.assertIn(b"403 Forbidden", client.recv(256))
            finally:
                server.shutdown()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


class ComposeBoundaryTests(unittest.TestCase):
    def fixture(self):
        services = {name: {"environment": {}, "cpus": 0.1, "mem_limit": 128000000, "logging": {"options": {"max-size": "1m"}}} for name in PERMITTED}
        services["api"]["depends_on"] = {"migrate": {"condition": "service_completed_successfully"}}
        services["api"]["environment"]["TELEGRAM_CONTROL_URL"] = "http://collector:8092"
        services["api"]["environment"]["FORWARDED_ALLOW_IPS"] = "172.30.253.2"
        services["api"]["networks"] = {"edge-private": {"ipv4_address": "172.30.253.3"}, "app-private": {}, "storage-private": {}}
        services["web"]["networks"] = {"edge-private": {"ipv4_address": "172.30.253.2"}, "egress": {}}
        services["hermes"]["environment"]["HERMES_PROVIDER_API_KEY"] = "fixture-only"
        services["hermes"]["environment"]["HERMES_STREAM_RETRIES"] = "0"
        return {"services": services, "networks": {"app-private": {"internal": True}, "storage-private": {"internal": True},
            "edge-private": {"internal": True, "ipam": {"config": [{"subnet": "172.30.253.0/28"}]}}}}

    def test_valid_minimal_service_boundary(self):
        self.assertEqual([], validate_compose(self.fixture(), Path.cwd()))

    def test_hermes_stream_network_retries_cannot_multiply_durable_attempts(self):
        for value in (None, "1", "2", "-1", "invalid", False):
            with self.subTest(value=value):
                document = self.fixture()
                environment = document["services"]["hermes"]["environment"]
                if value is None:
                    environment.pop("HERMES_STREAM_RETRIES")
                else:
                    environment["HERMES_STREAM_RETRIES"] = value
                self.assertTrue(any("流式瞬态重试" in message
                                    for message in validate_compose(document, Path.cwd())))

    def test_stream_retry_setting_is_hermes_only(self):
        document = self.fixture()
        document["services"]["review-worker"]["environment"]["HERMES_STREAM_RETRIES"] = "0"
        self.assertTrue(any("非白名单配置键" in message
                            for message in validate_compose(document, Path.cwd())))

    def test_collector_refresh_budgets_are_the_only_added_runtime_controls(self):
        expected = {
            "LIVE_ODDS_REFRESH_SECONDS", "PREGAME_NEAR_HOURS",
            "PREGAME_NEAR_REFRESH_SECONDS", "PREGAME_FAR_REFRESH_SECONDS",
        }
        self.assertTrue(expected.issubset(PERMITTED["collector"]))
        document = self.fixture()
        document["services"]["collector"]["environment"]["UNREVIEWED_SETTING"] = "fixture"
        self.assertTrue(any("非白名单配置键" in message for message in validate_compose(document, Path.cwd())))

    def test_provider_key_does_not_leak_into_api_or_collector(self):
        document = self.fixture()
        document["services"]["api"]["environment"]["HERMES_PROVIDER_API_KEY"] = "fixture-only"
        self.assertTrue(any("不应获取模型" in message for message in validate_compose(document, Path.cwd())))

    def test_external_mount_and_cycle_rejected(self):
        document = self.fixture()
        document["services"]["api"]["volumes"] = [{"type": "bind", "source": "/unapproved-fixture", "read_only": True}]
        document["services"]["migrate"]["depends_on"] = {"api": {"condition": "service_started"}}
        errors = validate_compose(document, Path.cwd())
        self.assertTrue(any("超出工作区" in message for message in errors))
        self.assertTrue(any("循环" in message for message in errors))


if __name__ == "__main__":
    unittest.main()
