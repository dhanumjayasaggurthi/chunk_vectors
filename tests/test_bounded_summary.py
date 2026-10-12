"""Bounded detail retention for million-document profile runs."""
import unittest
from logger import RunSummary


class BoundedSummaryTests(unittest.TestCase):
    def test_counts_remain_exact_after_detail_cap(self):
        summary = RunSummary(max_rows=100)
        for index in range(10000):
            summary.add("ERROR" if index % 100 == 0 else "SKIPPED",
                        "file_%s.pdf" % index, error="synthetic")
        self.assertEqual(summary.counts()["ERROR"], 100)
        self.assertEqual(summary.counts()["SKIPPED"], 9900)
        self.assertEqual(summary._total, 10000)
        self.assertEqual(len(summary._rows), 100)

    def test_legacy_keeps_complete_details(self):
        summary = RunSummary()
        for index in range(200):
            summary.add("SUCCESS", str(index))
        self.assertEqual(len(summary._rows), 200)


if __name__ == "__main__":
    unittest.main()
