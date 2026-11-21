"""Analyst decisions are append-only and rejected when they are malformed.

    PYTHONPATH=system python3 -m unittest screen.test_analyst_log
"""

from __future__ import annotations

import os
import tempfile
import unittest

from screen.analyst_log import history, record, unreadable_lines


class AnalystLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="analyst-log-")
        self.path = os.path.join(self.directory, "analyst_log.jsonl")

    def test_valid_decision_is_stored_and_survives_reopen(self):
        status, body = record(
            self.path, "H-1", "CONFIRM_MATCH", "Names and the 1980 date agree.", "Ada Quinn",
            notes="Checked the passport.", cited_ids=["KYC-1"], allowed_ids={"KYC-1"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["event"]["previous_action"], None)
        status, body = record(
            self.path, "H-1", "REOPEN", "The date of birth needs another look.", "Ada Quinn",
        )
        self.assertEqual(status, 201)
        events = history(self.path, "H-1")
        self.assertEqual([event["action"] for event in events], ["CONFIRM_MATCH", "REOPEN"])
        self.assertEqual(events[0]["cited_ids"], ["KYC-1"])
        self.assertTrue(events[1]["at"].endswith("Z"))
        # A new reader sees the same lines.
        self.assertEqual(len(history(self.path, "H-1")), 2)

    def test_invalid_requests_write_nothing(self):
        samples = [
            ("H-1", "APPROVE", "Long enough rationale.", "Ada"),
            ("H-1", "CLEAR_MATCH", "short", "Ada"),
            ("H-1", "ESCALATE", "Long enough rationale.", ""),
            ("H-MISSING", "CLEAR_MATCH", "Long enough rationale.", "Ada"),
            ("H-1", "REOPEN", "Long enough rationale.", "Ada"),
        ]
        for hit_id, action, rationale, analyst in samples:
            status, body = record(
                self.path, hit_id, action, rationale, analyst,
                hit_known=(hit_id != "H-MISSING"),
            )
            self.assertEqual(status, 400, body)
            self.assertIn("error", body)
        self.assertEqual(history(self.path, "H-1"), [])
        self.assertFalse(os.path.exists(self.path))

    def test_duplicate_key_and_same_state_do_not_append(self):
        status, first = record(
            self.path, "H-1", "CLEAR_MATCH", "Countries do not overlap.", "Ada Quinn",
            idempotency_key="key-1",
        )
        self.assertEqual(status, 201)
        status, second = record(
            self.path, "H-1", "ESCALATE", "This rationale should be ignored.", "Ada Quinn",
            idempotency_key="key-1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["event"]["event_id"], first["event"]["event_id"])
        status, third = record(
            self.path, "H-1", "CLEAR_MATCH", "Countries do not overlap.", "Bea Quinn",
        )
        self.assertEqual(third["status"], "unchanged")
        self.assertEqual(len(history(self.path, "H-1")), 1)

    def test_unknown_evidence_id_is_rejected(self):
        status, body = record(
            self.path, "H-1", "ESCALATE", "The model mentioned a record.", "Ada Quinn",
            cited_ids=["NOT-A-RECORD"], allowed_ids={"KYC-1"},
        )
        self.assertEqual(status, 400)
        self.assertIn("NOT-A-RECORD", body["error"])
        self.assertEqual(history(self.path, "H-1"), [])

    def test_a_corrupt_line_is_skipped_and_the_file_is_kept(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{broken\n")
            handle.write('{"hit_id": "H-1", "action": "ESCALATE", "rationale": "kept", "analyst": "Ada"}\n')
        self.assertEqual(unreadable_lines(self.path), [1])
        before = open(self.path, encoding="utf-8").read()
        status, body = record(self.path, "H-9", "CLEAR_MATCH", "A later readable decision.", "Ada Quinn")
        self.assertEqual(status, 201)
        self.assertEqual(body["unreadable_lines"], [1])
        after = open(self.path, encoding="utf-8").read()
        self.assertTrue(after.startswith(before))
        self.assertIn("{broken", after)
        self.assertEqual([event["hit_id"] for event in history(self.path, "H-1")], ["H-1"])
        self.assertEqual(len(history(self.path, "H-9")), 1)

    def test_notes_over_the_limit_are_rejected(self):
        status, body = record(
            self.path, "H-1", "ESCALATE", "Long enough rationale.", "Ada Quinn",
            notes="x" * 2001,
        )
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
