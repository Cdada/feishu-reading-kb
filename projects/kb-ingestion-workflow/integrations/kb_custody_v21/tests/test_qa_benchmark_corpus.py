import hashlib
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_benchmark_corpus import BenchmarkCorpus
from kb_custody_v21.qa_index import QAIndex


def selection(path='库/测量.md',**extra):
    return [dict(id='measure',path=path,title='轨道测量',role='original',canonical_id='orbit',**extra)]


class BenchmarkCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve(); self.path=self.root/'sample.zip'
        self.addCleanup(self.temp.cleanup)

    def write(self, entries):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.path,'w') as z:
                for name,body in entries: z.writestr(name,body)

    def test_reads_pinned_text_and_builds_separate_index_without_extracting(self):
        body='# 轨道测量\n\n轨道测量使用光学观测，误差范围需要分别记录。\n'
        self.write([('库/测量.md',body),('库/._测量.md',b'metadata')])
        before=self.path.read_bytes()
        with BenchmarkCorpus(self.path,selection(body_sha256=hashlib.sha256(body.encode()).hexdigest())) as c:
            source=c.get('measure')
            self.assertEqual(source['text'],body)
            self.assertIsNone(source['publication_year'])
            self.assertTrue(source['benchmark_only'])
            self.assertEqual(source['binding']['kind'],'read_only_benchmark')
            index=QAIndex(self.root/'isolated.sqlite3')
            self.assertEqual(index.sync(c)['total'],1)
            hit=index.search('轨道测量光学观测')[0]
            self.assertEqual(hit['text'],body[hit['char_start']:hit['char_end']])
        self.assertEqual(before,self.path.read_bytes())
        self.assertFalse((self.root/'库').exists())
        with self.assertRaises(ContractError):c.get('measure')

    def test_unflagged_utf8_member_names_are_decoded_explicitly(self):
        self.write([('库/测量.md','轨道测量正文')])
        raw=bytearray(self.path.read_bytes())
        for signature,offset in ((b'PK\x03\x04',6),(b'PK\x01\x02',8)):
            start=raw.index(signature)
            flags=int.from_bytes(raw[start+offset:start+offset+2],'little') & ~0x800
            raw[start+offset:start+offset+2]=flags.to_bytes(2,'little')
        self.path.write_bytes(raw)
        with BenchmarkCorpus(self.path,selection()) as c:self.assertEqual(c.get('measure')['text'],'轨道测量正文')

    def test_bad_names_and_ambiguous_names_are_rejected(self):
        for names in (['../evil.md'],['/evil.md'],['a\\b.md'],['a.md','A.md'],['a.md','a.md']):
            self.write([(n,'正文') for n in names]+[('库/测量.md','正文')])
            with self.subTest(names=names),self.assertRaises(ContractError):
                with BenchmarkCorpus(self.path,selection()):pass

    def test_symbolic_link_rejected_even_when_not_selected(self):
        info=zipfile.ZipInfo('link'); info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
        self.write([('库/测量.md','正文'),(info,'target')])
        with self.assertRaises(ContractError):
            with BenchmarkCorpus(self.path,selection()):pass

    def test_invalid_text_digest_and_limits_fail_before_indexing(self):
        for body,kwargs,extra in ((b'\xff',{},{}),('\x00',{},{}),('  ',{},{}),
                ('正文',{},dict(body_sha256='0'*64)),('正文',dict(max_entry_bytes=2),{}),
                ('正文',dict(max_total_bytes=2),{})):
            self.write([('库/测量.md',body)])
            with self.subTest(body=body,kwargs=kwargs),self.assertRaises(ContractError):
                with BenchmarkCorpus(self.path,selection(**extra),**kwargs) as c:c.get('measure')

    def test_selection_does_not_admit_metadata_duplicates_or_unsupported_files(self):
        for rows in (selection('库/._file.md'),selection('库/file.pdf'),selection()+selection(),
                     selection('库/../file.md'),selection(role_override='invented')):
            with self.subTest(rows=rows),self.assertRaises(ContractError):BenchmarkCorpus(self.path,rows)
        self.write([('other.md','正文')])
        with self.assertRaises(ContractError):
            with BenchmarkCorpus(self.path,selection()):pass

    def test_failure_closes_archive_and_context_can_be_reopened(self):
        self.write([('库/测量.md','正文')])
        c=BenchmarkCorpus(self.path,selection())
        for _ in range(2):
            with c:self.assertEqual(c.ids(),['measure'])
        with self.assertRaises(ContractError):c.ids()


if __name__=='__main__':unittest.main()
