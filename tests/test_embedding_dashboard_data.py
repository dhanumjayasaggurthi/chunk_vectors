"""Unit checks for read-only embedding dashboard status logic."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import embedding_dashboard_data as dashboard


class DashboardLogicTests(unittest.TestCase):
    def test_active_claims_take_precedence(self):
        state = dashboard.summarize_job(
            {"discovery_done": True},
            {"PENDING": 10, "RUNNING": 2, "SUCCESS": 8, "FAILED": 3},
            active_claims=2,
        )
        self.assertEqual(state["phase"], "Active claims")
        self.assertEqual(state["total"], 23)
        self.assertEqual(state["processed"], 11)
        self.assertAlmostEqual(state["progress"], 11/23)

    def test_terminal_errors_are_not_silent(self):
        state = dashboard.summarize_job(
            {"discovery_done": True}, {"SUCCESS": 991, "FAILED": 9}
        )
        self.assertEqual(state["phase"], "Completed with failures")
        self.assertEqual(state["progress"], 1.0)

    def test_incomplete_discovery_is_not_marked_completed(self):
        state = dashboard.summarize_job(
            {"discovery_done": False}, {"SUCCESS": 40}
        )
        self.assertEqual(state["phase"], "Discovering / awaiting worker")

    def test_expired_leases_are_not_live_claims(self):
        state = dashboard.summarize_job(
            {"discovery_done": True}, {"SUCCESS": 10, "RUNNING": 1},
            active_claims=0,
        )
        self.assertEqual(state["phase"], "Pending / no active lease")

    def test_only_latest_30_jobs_requested(self):
        connection = type("FakeConnection", (), {"close": lambda self: None})()
        seen_queries = []
        def fake_rows(conn, sql, params=()):
            seen_queries.append((sql, params))
            if "information_schema.columns" in sql:
                return []
            if "FROM embedding_runtime.runs r" in sql:
                return []
            if "current_database()" in sql:
                return [{"name": "testdb"}]
            self.fail("Unexpected query")
        with patch.object(dashboard, "connect_readonly", return_value=connection), \
             patch.object(dashboard, "_rows", side_effect=fake_rows):
            result = dashboard.fetch_overview(9999)
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["database"], "testdb")
        self.assertEqual([params for sql, params in seen_queries
                          if "FROM embedding_runtime.runs r" in sql], [(30,)])

    def test_no_job_is_terminal_with_zero_tasks(self):
        state = dashboard.summarize_job({"discovery_done": True},{})
        self.assertEqual(state["phase"],"No tasks discovered")
        self.assertEqual(state["progress"],0.0)


if __name__=="__main__":
    unittest.main()
