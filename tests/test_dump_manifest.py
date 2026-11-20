import io
import unittest
from infra.storage.dump_manifest import copy_manifest


class CopyManifestTests(unittest.TestCase):
    def test_counts_rows_and_empty_tables_without_exporting_contents(self):
        value = copy_manifest(io.BytesIO(
            b'-- PostgreSQL data dump\nCOPY public.v2_messages (id, content) FROM stdin;\n'
            b'1\tprivate\\ntext\n2\t\\N\n\\.\n'
            b'COPY public.v2_empty (id) FROM stdin;\n\\.\n'))
        self.assertEqual(2, value['tables']['v2_messages']['rows'])
        self.assertEqual(0, value['tables']['v2_empty']['rows'])
        self.assertFalse(value['contains_row_contents'])
        self.assertNotIn('private', repr(value))
        self.assertEqual(64, len(value['tables']['v2_messages']['ordered_copy_sha256']))

    def test_rejects_incomplete_unknown_duplicate_and_empty_streams(self):
        for raw in (
            b'COPY public.v2_rows (id) FROM stdin;\nsecret',
            b'COPY public.legacy_rows (id) FROM stdin;\n',
            b'COPY public.v2_rows (id) FROM stdin;\n\\.\nCOPY public.v2_rows (id) FROM stdin;\n',
            b'-- no table copy\n',
        ):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                copy_manifest(io.BytesIO(raw))

    def test_canonical_multiset_hash_handles_row_and_column_order(self):
        a=copy_manifest(io.BytesIO(b'COPY public.v2_rows (id, text) FROM stdin;\n1\ta\n2\tb\n\\.\n'))['tables']['v2_rows']
        b=copy_manifest(io.BytesIO(b'COPY public.v2_rows (text, id) FROM stdin;\nb\t2\na\t1\n\\.\n'))['tables']['v2_rows']
        self.assertNotEqual(a['ordered_copy_sha256'],b['ordered_copy_sha256'])
        for key in ('rows','columns','row_sha256_sum_mod_2_256','row_sha256_xor'):
            self.assertEqual(a[key],b[key])
        changed=copy_manifest(io.BytesIO(b'COPY public.v2_rows (id, text) FROM stdin;\n1\ta\n2\ta\n\\.\n'))['tables']['v2_rows']
        self.assertNotEqual(a['row_sha256_sum_mod_2_256'],changed['row_sha256_sum_mod_2_256'])
