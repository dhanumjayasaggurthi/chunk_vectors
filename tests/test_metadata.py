import unittest
from mir_ai.metadata import overlap_with_rimdocs
from mir_ai.metadata_schema import FIELDS
class TestMetadata(unittest.TestCase):
 def test_schema_has_exactly_50_visible_fields(self):self.assertEqual(len(FIELDS),50);self.assertEqual([f for f in FIELDS if f.key=="vehicle_type"][0].source_label,"vehicle type")
 def test_rimdocs_authoritative_skips_extraction(self):
  r,m=overlap_with_rimdocs({"Study ID":"ABC","compound_number":"JNJ-1","Unknown":"ignored"});self.assertEqual(r["study_id"].value,"ABC");self.assertEqual(r["study_id"].source,"rimdocs");self.assertNotIn("study_id",m);self.assertNotIn("compound_number",m);self.assertEqual(len(r)+len(m),50)
