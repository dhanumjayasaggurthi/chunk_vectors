import tempfile,unittest,zipfile
from mir_ai.docx_stream import DOCXPageStream
class TestDocxTable(unittest.TestCase):
 def test_table_rows_preserved(self):
  xml='''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'''
  with tempfile.NamedTemporaryFile(suffix='.docx') as tmp:
   with zipfile.ZipFile(tmp.name,'w') as z:z.writestr('word/document.xml',xml)
   p=list(DOCXPageStream(tmp.name,'d'))
  t=[e for e in p[0].elements if e.element_type=='table'];self.assertEqual(t[0].raw['rows'],[['A','B'],['1','2']]);self.assertIn('| A | B |',t[0].text)
