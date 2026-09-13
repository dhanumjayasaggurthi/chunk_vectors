import unittest
from mir_ai.bounded import ContiguousProgress
class TestProgress(unittest.TestCase):
 def test_out_of_order_does_not_skip_gap(self):
  p=ContiguousProgress(10);self.assertEqual(p.mark(12),10);self.assertEqual(p.mark(13),10);self.assertEqual(p.mark(11),13);self.assertEqual(p.mark(15),13);self.assertEqual(p.mark(14),15)
