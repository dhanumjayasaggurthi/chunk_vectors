import unittest
from mir_ai.metadata import MetadataExtractor
from mir_ai.models import PageRecord
class FakeExtractor(MetadataExtractor):
 def __init__(self):self.calls=0
 def extract_missing(self,keys,pages):
  from mir_ai.models import MetadataValue
  self.calls+=1;out={}
  for k in keys:out[k]=MetadataValue(k,"Study ID" if k=="study_id" else k,"S-1" if k=="study_id" and self.calls==1 else None,source="llm" if k=="study_id" and self.calls==1 else "missing",status="extracted" if k=="study_id" and self.calls==1 else "missing",evidence_text="S-1" if k=="study_id" and self.calls==1 else "",evidence_pages=[pages[0].page_number] if k=="study_id" and self.calls==1 else [])
  return out
class TestMetadataStream(unittest.TestCase):
 def test_resolved_fields_not_re_requested(self):
  pages=(PageRecord(i,str(i),0,0,native_text=f"p{i}") for i in range(1,14));ex=FakeExtractor();r=ex.extract_missing_stream(["study_id","compound_number"],pages,3);self.assertEqual(r["study_id"].value,"S-1");self.assertEqual(r["compound_number"].status,"missing");self.assertGreater(ex.calls,1)
