import threading,time,unittest
from mir_ai.bounded import bounded_parallel_map
class TestBoundedMap(unittest.TestCase):
 def test_never_exceeds_inflight_budget(self):
  active=0;peak=0;lock=threading.Lock()
  def work(x):
   nonlocal active,peak
   with lock:active+=1;peak=max(peak,active)
   time.sleep(.001)
   with lock:active-=1
   return x
  out=list(bounded_parallel_map(work,range(200),4,7));self.assertEqual(sorted(out),list(range(200)));self.assertLessEqual(peak,4)
