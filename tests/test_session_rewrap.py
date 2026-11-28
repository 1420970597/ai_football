"""Offline synthetic StringSession rewrap only; no Telegram connection."""
import base64
import copy
import json
import unittest

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from infra.recovery.bundle import RecoveryError
from infra.recovery.session_rewrap import rewrap_session
from services.api.app.security import decrypt_secret


class SessionRewrapTests(unittest.TestCase):
    def setUp(self):
        session = StringSession()
        session.set_dc(2, '149.154.167.51', 443)
        session.auth_key = AuthKey(bytes(range(256)))
        self.session = session.save()
        self.old_key = base64.urlsafe_b64encode(b'o'*32)
        self.new_key = b'n'*32
        self.ciphertext = Fernet(self.old_key).encrypt(self.session.encode())
        self.options = {'old_key': self.old_key, 'new_key': self.new_key,
                        'source_algorithm': 'fernet', 'source_version': 1, 'target_version': 1,
                        'source_key_reference': 'keyref:legacy/telegram/1',
                        'target_key_reference': 'keyref:v2/data/1'}

    def test_explicit_keys_canonical_session_and_current_algorithm_roundtrip(self):
        before = copy.deepcopy((self.ciphertext, self.options))
        result = rewrap_session(self.ciphertext, **self.options, expected_session=self.session)
        self.assertEqual('telegram-session-rewrap-1', result['format'])
        self.assertEqual('fernet', result['source']['algorithm'])
        self.assertEqual('aes-256-gcm', result['target']['algorithm'])
        self.assertTrue(result['validation']['separate_source_matches'])
        self.assertFalse(result['validation']['telegram_authorization_verified'])
        self.assertEqual(self.session, decrypt_secret(self.new_key, result['target']['ciphertext']))
        payload = base64.urlsafe_b64decode(result['target']['ciphertext'])
        self.assertEqual(self.session.encode(), AESGCM(self.new_key).decrypt(payload[:12],payload[12:],None))
        self.assertEqual(before,(self.ciphertext,self.options))
        self.assertFalse(result['keys_generated'])
        self.assertFalse(result['files_written'])
        self.assertFalse(result['live_session_replaced'])

    def test_envelope_contains_no_plain_session_or_key_and_nonce_is_fresh(self):
        first = rewrap_session(self.ciphertext, **self.options)
        second = rewrap_session(self.ciphertext, **self.options)
        self.assertNotEqual(first['target']['ciphertext'],second['target']['ciphertext'])
        output = json.dumps(first)
        for secret in (self.session,self.old_key.decode(),self.new_key.decode()):
            self.assertNotIn(secret,output)
        self.assertIsNone(first['validation']['separate_source_matches'])

    def test_wrong_key_and_modified_ciphertext_fail_without_secret_in_error(self):
        for ciphertext, updates in ((self.ciphertext,{'old_key':base64.urlsafe_b64encode(b'x'*32)}),
                                    (self.ciphertext[:-10]+b'corruption',{})):
            with self.assertRaisesRegex(RecoveryError,'legacy_session_decryption_failed') as raised:
                rewrap_session(ciphertext,**(self.options | updates))
            self.assertNotIn(self.session,str(raised.exception))
            self.assertNotIn(self.old_key.decode(),str(raised.exception))

    def test_same_material_or_same_reference_rejected(self):
        for updates in ({'new_key': b'o'*32},
                        {'target_key_reference':self.options['source_key_reference']}):
            with self.assertRaisesRegex(RecoveryError,'independent_source_and_target'):
                rewrap_session(self.ciphertext,**(self.options | updates))

    def test_unknown_versions_algorithm_or_implicit_keys_rejected(self):
        for updates in ({'source_algorithm':'aes-gcm'}, {'source_version':2}, {'source_version':True},
                        {'target_version':0}, {'target_version':'1'}, {'old_key':None},
                        {'new_key':b'short'}, {'old_key':b'not-fernet'}, {'new_key':self.new_key.hex()},
                        {'source_key_reference':'/root/secret'}, {'target_key_reference':''}):
            with self.subTest(updates=updates):
                with self.assertRaises(RecoveryError):
                    rewrap_session(self.ciphertext,**(self.options | updates))

    def test_decrypted_invalid_noncanonical_empty_or_unbound_session_rejected(self):
        for plaintext in ('','not-a-telegram-session',' '+self.session,self.session+' ',StringSession().save()):
            ciphertext = Fernet(self.old_key).encrypt(plaintext.encode())
            with self.assertRaisesRegex(RecoveryError,'legacy_session_structure_invalid'):
                rewrap_session(ciphertext,**self.options)
        for ciphertext in (b'',b'x'*8193,'not-bytes'):
            with self.assertRaisesRegex(RecoveryError,'legacy_session_ciphertext_budget_invalid'):
                rewrap_session(ciphertext,**self.options)

    def test_separately_obtained_session_mismatch_fails_instead_of_overwriting(self):
        other = StringSession(self.session)
        other.auth_key = AuthKey(b'x'*256)
        with self.assertRaisesRegex(RecoveryError,'legacy_session_source_comparison_failed'):
            rewrap_session(self.ciphertext,**self.options,expected_session=other.save())
