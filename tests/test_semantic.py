import unittest
from mir_ai.models import BBox,ElementRef,PageRecord,SemanticUnit
from mir_ai.semantic import Chunker,SemanticUnitStream,repeated_signatures
class FixedTokens:
 def count(self,text):return len(text.split())
def page(n,text,header="",footer=""):return PageRecord(n,str(n),600,800,[ElementRef(f"e{n}","text",BBox(20,100,580,700),0,text)],text,header,footer)
class TestSemantic(unittest.TestCase):
 def test_repeated_headers(self):
  h,_=repeated_signatures([page(i,f"body {i}",header=f"Study ABC - page {i}") for i in range(1,8)],.5);self.assertEqual(len(h),1)
 def test_atomic_unit_is_not_split(self):
  u=SemanticUnit("u","table","x "*2000,1,2,["1","2"],atomic=True);c=list(Chunker("d","g",1200,1500,100,FixedTokens()).chunks([u]));self.assertEqual(len(c),1);self.assertTrue(c[0].metadata["oversize_atomic"]);self.assertGreater(FixedTokens().count(c[0].text),1500)
 def test_12000_pages_stream_without_materializing_pages(self):
  def pages():
   for i in range(1,12001):yield page(i,"one two three four five")
  c=list(Chunker("d","g",30,40,5,FixedTokens()).chunks(SemanticUnitStream("d").from_pages(pages())));self.assertGreater(len(c),1000);self.assertEqual(c[0].page_start,1);self.assertEqual(c[-1].page_end,12000)
