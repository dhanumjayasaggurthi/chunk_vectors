import unittest
from mir_ai.vision import VisionResult
class TestVisionResult(unittest.TestCase):
 def test_low_confidence_can_retain_text(self):
  r=VisionResult('123 456',.4,2,'en',True,'low_confidence');self.assertEqual(r.text,'123 456');self.assertTrue(r.success)
