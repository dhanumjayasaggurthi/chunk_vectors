import tempfile,unittest,zipfile
from mir_ai.docx_stream import DOCXPageStream
class TestDocxStream(unittest.TestCase):
 def test_explicit_page_break_is_logical_page_boundary(self):
  xml='''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First page</w:t></w:r><w:r><w:br w:type="page"/></w:r></w:p><w:p><w:r><w:t>Second page</w:t></w:r></w:p></w:body></w:document>'''
  with tempfile.NamedTemporaryFile(suffix=".docx") as tmp:
   with zipfile.ZipFile(tmp.name,"w") as z:z.writestr("word/document.xml",xml)
   p=list(DOCXPageStream(tmp.name,"doc"))
  self.assertEqual(len(p),2);self.assertIn("First page",p[0].native_text);self.assertIn("Second page",p[1].native_text);self.assertIsNone(p[0].elements[0].bbox)
