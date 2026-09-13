import time,unittest
from mir_ai.bounded import bounded_ordered_map
class TestOrdered(unittest.TestCase):
 def test_preserves_source_order(self):
  def work(x):time.sleep((5-x)*.001 if x<5 else 0);return x
  self.assertEqual(list(bounded_ordered_map(work,range(10),4,6)),list(range(10)))
