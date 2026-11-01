from copy import deepcopy
import hashlib
import unittest
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.reading_contract import request, sha
from kb_custody_v21.reading_input_storage import pack, unpack
from kb_custody_v21.registry import encoded
from test_reading_content import sample

class StorageTests(unittest.TestCase):
    def value(self):
        source,_=sample();text='合成知识内容。'*500
        path='20_Cards/synthetic.md'
        source['existing_cards'][path]=dict(sha256=hashlib.sha256(text.encode()).hexdigest(),text=text)
        source['excerpts']=source['original']
        return dict(source=source,existing_files={path:text},available_paths=[path],date='2026-01-01')

    def test_lossless_smaller_snapshot_preserves_exact_source_hash(self):
        value=self.value();before=deepcopy(value);packed=pack(value)
        self.assertLess(len(encoded(packed)),len(encoded(value))*0.65)
        self.assertEqual(unpack(packed),value);self.assertEqual(value,before)
        self.assertEqual(sha(unpack(packed)['source']),sha(value['source']))
        # Repeated reads are idempotent; no live library or mutable source lookup.
        self.assertEqual(unpack(packed),unpack(packed))

    def test_old_format_remains_readable_and_distinct_excerpt_is_preserved(self):
        value=self.value();value['source']['excerpts']='单独的合成摘录。'
        self.assertEqual(unpack(value),value)
        self.assertEqual(unpack(pack(value)),value)

    def test_damaged_snapshot_missing_dependency_or_unknown_format_blocks(self):
        for kind in ('text','digest','missing','format','excerpt'):
            value=pack(self.value())
            if kind=='text':value['saved']['existing_files']['20_Cards/synthetic.md']='changed'
            if kind=='digest':value['source_sha256']='0'*64
            if kind=='missing':value['saved']['existing_files']={}
            if kind=='format':value['format']='future'
            if kind=='excerpt':value['excerpt_reuses_original']='yes'
            with self.assertRaises(ContractError):unpack(value)

    def test_disagreeing_duplicate_is_not_silently_normalized(self):
        value=self.value();value['existing_files']['20_Cards/synthetic.md']='different'
        with self.assertRaises(ContractError):pack(value)

    def test_pre_pointer_crash_reuses_old_bytes_and_rejects_changed_plan(self):
        import tempfile
        from pathlib import Path
        from kb_custody_v21.reading_input_storage import save
        with tempfile.TemporaryDirectory() as root:
            path=Path(root).resolve()/'input.local.json'
            value=self.value();raw=encoded(value).encode();path.write_bytes(raw)
            save(path,value);self.assertEqual(path.read_bytes(),raw)
            changed=deepcopy(value);changed['source']['original']+='changed'
            with self.assertRaises(ContractError):save(path,changed)
            self.assertEqual(path.read_bytes(),raw)
