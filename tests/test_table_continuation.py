import unittest
from mir_ai.models import BBox,ElementRef,PageRecord
from mir_ai.semantic import SemanticUnitStream
def t(eid,bbox,rows):return ElementRef(eid,"table",bbox,0,"table",extraction_status="success",raw={"rows":rows,"col_count":len(rows[0]),"page_height":800})
class TestTableContinuation(unittest.TestCase):
 def test_stitches_only_conservative_continuation(self):
  p1=PageRecord(1,"1",600,800,[t("t1",BBox(10,600,590,790),[["A","B"],["1","2"]])],"");p2=PageRecord(2,"2",600,800,[t("t2",BBox(10,5,590,300),[["A","B"],["3","4"]])],"");u=list(SemanticUnitStream("d").from_pages([p1,p2]));tables=[x for x in u if x.unit_type=="table"];self.assertEqual(len(tables),1);self.assertEqual(tables[0].page_end,2);self.assertIn("3",tables[0].text)
