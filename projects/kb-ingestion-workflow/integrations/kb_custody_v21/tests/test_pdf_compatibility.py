"""Small generated PDFs; no real articles, network or model calls."""
import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from kb_custody_v21.pdf_compatibility import read_pdf, REFERENCE
from kb_custody_v21.mineru_chunks import PdfChunks, PdfOptions, file_digest
from test_mineru_chunks import SyntheticMineru


def fixture(fields=b'/MediaBox [0 0 200 300]\n/MediaBox [0 0 200 300]\n'):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R]\n'+fields+b'>>',
        b'<< /Type /Page /Parent 2 0 R /Resources <<>> /Contents 5 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /Resources <<>> /Contents 5 0 R >>',
        b'<< /Length 0 >>\nstream\n\nendstream']
    raw=b'%PDF-1.4\n'; offsets=[]
    for i,value in enumerate(objects,1):
        offsets.append(len(raw)); raw+=f'{i} 0 obj\n'.encode()+value+b'\nendobj\n'
    xref=len(raw)
    raw+=b'xref\n0 6\n0000000000 65535 f \n'
    raw+=b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
    return raw+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); self.source=self.root/'source.pdf'

    def read(self,raw):
        self.source.write_bytes(raw)
        return read_pdf(self.source)

    def test_identical_duplicate_repairs_only_the_error_span_and_keeps_both_pages(self):
        raw=fixture()
        with self.assertRaises(PdfReadError): len(PdfReader(io.BytesIO(raw),strict=True).pages)
        reader,audit,derived=self.read(raw)
        self.assertEqual(len(reader.pages),2)
        self.assertEqual([list(p.mediabox) for p in reader.pages],[[0,0,200,300]]*2)
        self.assertEqual(audit['reference'],REFERENCE)
        self.assertEqual(self.source.read_bytes(),raw)
        self.assertEqual(len(raw),len(derived))
        change=audit['changes'][0]; a,b=change['start'],change['end']
        self.assertEqual(derived,raw[:a]+b' '*(b-a)+raw[b:])
        self.assertEqual(len(PdfReader(io.BytesIO(derived),strict=True).pages),2)

    def test_valid_pdf_has_no_fallback_or_changed_bytes(self):
        raw=fixture(b'/MediaBox [0 0 200 300]\n')
        reader,audit,derived=self.read(raw)
        self.assertIsNone(audit); self.assertEqual(derived,raw)
        self.assertEqual(len(reader.pages),2)

    def test_numeric_equivalence_and_adjacent_repeated_fields(self):
        reader,audit,_=self.read(fixture(b'/MediaBox [0 0 200 300]\n/MediaBox [0.0 0 200.0 300]\n/MediaBox [0 0 200 300]\n'))
        self.assertEqual(len(reader.pages),2); self.assertEqual(len(audit['changes']),2)

    def test_conflicting_nonadjacent_and_other_duplicate_fields_stay_errors(self):
        cases=[b'/MediaBox [0 0 200 300]\n/MediaBox [0 0 400 500]\n',
            b'/MediaBox [0 0 200 300]\n/Rotate 0\n/MediaBox [0 0 200 300]\n',
            b'/MediaBox [0 0 200 300]\n/Rotate 90\n/Rotate 90\n']
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(PdfReadError): self.read(fixture(fields))

    def test_comment_cannot_hide_a_conflicting_previous_value(self):
        fields=b'/MediaBox [0 0 400 500]\n% /MediaBox [0 0 200 300]\n/MediaBox [0 0 200 300]\n'
        with self.assertRaises(PdfReadError): self.read(fixture(fields))

    def test_second_unrelated_corruption_is_not_accepted_after_a_repair(self):
        fields=b'/MediaBox [0 0 200 300]\n/MediaBox [0 0 200 300]\n/Rotate 90\n/Rotate 0\n'
        with self.assertRaises(PdfReadError): self.read(fixture(fields))

    def test_unrelated_invalid_pdf_remains_a_failure(self):
        with self.assertRaises(PdfReadError): self.read(b'not a PDF')

    def test_conversion_retains_audit_and_emits_compatibility_without_new_model(self):
        raw=fixture(); self.source.write_bytes(raw); digest=file_digest(self.source)
        converter=SyntheticMineru(); events=[]
        engine=PdfChunks(self.root/'chunks',PdfOptions(),converter)
        output=engine.convert(self.source,digest,progress=events.append)
        self.assertTrue((output/'source.md').is_file())
        self.assertEqual(converter.calls,[('p0001-0002','auto')])
        self.assertTrue((self.root/'chunks'/digest/'parser-compatibility.json').is_file())
        self.assertEqual(self.source.read_bytes(),raw)
        self.assertEqual(events[-1]['completed_pages'],2)
        self.assertTrue(all(e['compatibility']==REFERENCE for e in events))
        engine.convert(self.source,digest)
        self.assertEqual(len(converter.calls),1)


if __name__=='__main__': unittest.main()
