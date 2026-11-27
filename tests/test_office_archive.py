"""DOCX archive limits keep compressed documents from expanding without bound inside TextFold."""
import io
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from textfold import office


DOCUMENT = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{office.W[1:-1]}" xmlns:m="{office.omml.M[1:-1]}">
  <w:body><w:p><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></w:p></w:body>
</w:document>'''.encode()


class DocxArchiveTest(unittest.TestCase):
    def archive(self, members):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "document.docx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in members:
                z.writestr(name, data)
        self.addCleanup(tmp.cleanup)
        return path

    def test_declared_archive_budgets_are_enforced(self):
        cases = [
            ("member count", [("a", b"a"), ("b", b"b")], {"DOCX_MAX_MEMBERS": 1}),
            ("member size", [("a", b"123456789")], {"DOCX_MAX_MEMBER_BYTES": 8}),
            ("total size", [("a", b"123456"), ("b", b"123456")], {"DOCX_MAX_TOTAL_BYTES": 10}),
            ("compression ratio", [("a", b"a" * 4096)],
             {"DOCX_MAX_COMPRESSION_RATIO": 2, "DOCX_RATIO_MIN_BYTES": 0}),
            ("formula XML size", [("word/document.xml", b"123456789")], {"DOCX_MAX_MATH_BYTES": 8}),
        ]
        for title, members, limits in cases:
            with self.subTest(title), mock.patch.multiple(office, **limits):
                with self.assertRaisesRegex(ValueError, "bad-file"):
                    office._with_math(self.archive(members))

    def test_streamed_size_is_rechecked(self):
        info = zipfile.ZipInfo("word/document.xml")
        info.file_size = info.compress_size = 3
        archive = mock.Mock()
        archive.open.return_value = nullcontext(io.BytesIO(b"123456"))
        with mock.patch.object(office, "DOCX_MAX_MEMBER_BYTES", 4), self.assertRaisesRegex(ValueError, "bad-file"):
            office._copy_docx_member(archive, info, io.BytesIO(), 0)

    def test_formula_conversion_and_other_members_survive(self):
        image = b"ordinary image bytes"
        with office._with_math(self.archive([("word/document.xml", DOCUMENT),
                                             ("word/media/image.png", image)])) as rebuilt:
            with zipfile.ZipFile(rebuilt) as z:
                document = "".join(office._xml(z.read("word/document.xml")).itertext())
                self.assertIn(f"{office.IN}x{office.OUT}", document)
                self.assertEqual(z.read("word/media/image.png"), image)


if __name__ == "__main__":
    unittest.main()
