"""Native App signing: NIST vectors, initialization and error handling."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from collector.leyu_app_signing import NativeAppSigner, signer_from_env
from collector.leyu_app_login import login_provider_from_env
from collector.leyu_client import DecodeError, _aes_ecb_decrypt_pure, encrypt_aes_cbc
from collector.session import SessionError


def routes():
    return {p: {"key": "k" * 32, "iv": "i" * 16}
            for p in ("", "/site/api", "/game/api")}


def decrypt(data, key, iv):
    plain = bytearray()
    for off in range(0, len(data), 16):
        block = data[off:off + 16]
        plain.extend(a ^ b for a, b in zip(_aes_ecb_decrypt_pure(block, key), iv))
        iv = block
    return bytes(plain[:-plain[-1]])


class NativeAppSigningTest(unittest.TestCase):
    def test_nist_cbc_vectors_and_padding(self):
        # NIST SP 800-38A F.2.1 / F.2.5; two blocks verify CBC chaining.
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        for key, expected in (
            ("2b7e151628aed2a6abf7158809cf4f3c",
             "7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"),
            ("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4",
             "f58c4c04d6e5f1ba779eabfb5f7bfbd69cfc4e967edb808d679f777bc6702c7d"),
        ):
            encoded = encrypt_aes_cbc(plain, bytes.fromhex(key), iv)
            self.assertEqual(encoded[:32].hex(), expected)
            self.assertEqual(len(encoded), 48)
            self.assertEqual(decrypt(encoded, bytes.fromhex(key), iv), plain)
        self.assertEqual(len(encrypt_aes_cbc(b"", b"k" * 32, b"i" * 16)), 16)
        with self.assertRaises(DecodeError):
            encrypt_aes_cbc(b"", b"short", b"i" * 16)
        with self.assertRaises(DecodeError):
            encrypt_aes_cbc(b"", b"k" * 32, b"short")

    def test_headers_use_timestamp_and_initialization_ip(self):
        r = routes()
        r["/game/api"]["key"] = "g" * 32
        s = NativeAppSigner(r)
        s._ip = "203.0.113.5"
        with mock.patch("time.time", return_value=1700000000.5), mock.patch("secrets.randbelow", return_value=123):
            h = s.headers("/site/api")
            game = s.headers("/game/api")
        self.assertNotEqual(h["x-api-xxx"], game["x-api-xxx"])
        self.assertEqual(decrypt(bytes.fromhex(h["x-api-xxx"]), b"k" * 32, b"i" * 16), b"1700000000500223")
        binding = decrypt(base64.b64decode(h["x-api-hack-xxxxx"]), b"k" * 32, b"i" * 16)
        self.assertEqual(json.loads(binding), {"tt": "1700000000", "xx": "203.0.113.5"})

    def test_bootstrap_is_shared_and_only_fetches_public_configuration(self):
        s = NativeAppSigner(routes())
        with mock.patch("urllib.request.urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({"status_code": 6000, "data": {"ip": "203.0.113.5"}}).encode()
            s.initialize("https://app.test", "device", 3)
            s.initialize("https://app.test", "device", 3)
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.args[0]
        self.assertTrue(sent.full_url.endswith("/configuration/preInfo"))
        self.assertEqual(sent.data, b"{}")
        self.assertEqual(s._ip, "203.0.113.5")

    def test_rejected_bootstrap_does_not_mark_initialized_or_echo_secrets(self):
        s = NativeAppSigner(routes())
        with mock.patch("urllib.request.urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({"status_code": 6003, "message": "private-token"}).encode()
            with self.assertRaises(SessionError) as error:
                s.initialize("https://app.test", "device", 3)
        self.assertNotIn("private-token", str(error.exception))
        self.assertFalse(s._initialized)

    def test_private_configuration_is_optional_and_validated(self):
        self.assertIsNone(signer_from_env({}))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"routes": routes()}))
            self.assertIsInstance(signer_from_env({"LEYU_APP_SIGNING_CONFIG": str(p)}), NativeAppSigner)
            p.write_text('{"routes": {}}')
            with self.assertRaises(SessionError):
                signer_from_env({"LEYU_APP_SIGNING_CONFIG": str(p)})

    def test_native_login_launch_and_both_expiry_paths(self):
        """业务过期仅 launch；App token 过期才登录，共享一次初始化。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signing.json"
            path.write_text(json.dumps({"routes": routes()}))
            provider = login_provider_from_env({
                "LEYU_APP_LOGIN_NAME": "example-user", "LEYU_APP_LOGIN_PASSWORD": "example-pwd",
                "LEYU_APP_UUID": "example-device", "LEYU_APP_HOST": "https://app.test",
                "LEYU_APP_SIGNING_CONFIG": str(path),
                "LEYU_APP_LOGIN_STATE": str(Path(directory) / "guard.json"),
            })
            self.assertIsNotNone(provider)
            assert provider is not None
            requests = []
            replies = iter([
                {"status_code": 6000, "data": {"ip": "203.0.113.5"}},
                {"status_code": 6000, "data": {"token": "T1"}},
                {"status_code": 6000, "data": {"url": "https://api.test?token=RID1"}},
                {"status_code": 6000, "data": {"url": "https://api.test?token=RID2"}},
                {"status_code": 6001, "data": {}},
                {"status_code": 6000, "data": {"token": "T2"}},
                {"status_code": 6000, "data": {"url": "https://api.test?token=RID3"}},
            ])

            def reply(request, **kwargs):
                requests.append(request)
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(next(replies)).encode()
                return response

            with mock.patch("urllib.request.urlopen", side_effect=reply):
                with mock.patch("time.monotonic", return_value=100), \
                        mock.patch("time.time", return_value=100):
                    first = provider.acquire()
                    provider.invalidate(first)
                    self.assertEqual(provider.acquire().request_id, "RID2")
                with mock.patch("time.monotonic", return_value=200), \
                        mock.patch("time.time", return_value=200):
                    provider.invalidate(first)
                    self.assertEqual(provider.acquire().request_id, "RID3")
            self.assertEqual(provider.logins, 2)
            self.assertEqual(sum(r.full_url.endswith("/preInfo") for r in requests), 1)
            self.assertEqual(sum(r.full_url.endswith("/user/login") for r in requests), 2)
            launch = requests[2]
            headers = {k.lower(): v for k, v in launch.headers.items()}
            body = json.loads(launch.data)
            self.assertEqual(headers["x-api-client"], "sport_android")
            self.assertEqual(headers["x-inter-client"], "android")
            self.assertEqual(body["sendMoney"], "false")
            self.assertEqual(body["nativeApp"], "1")
            self.assertFalse(body["isUseCache"])
            self.assertIn("x-api-hack-xxxxx", headers)
            self.assertIs(provider.launcher.signer, provider.client.signer)
