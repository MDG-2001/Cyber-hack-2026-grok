"""Dashboard arithmetic and the name-only baseline.

    PYTHONPATH=system python3 -m unittest screen.test_dashboard
"""

from __future__ import annotations

import os
import unittest

from screen.dashboard import (
    alert_outcome,
    list_type,
    name_only_decision,
    practice_metrics,
    summarise,
)
from screen.names import NameIndex


class DashboardMathTests(unittest.TestCase):
    def test_alert_outcome(self):
        self.assertEqual(alert_outcome(["DIFFERENT_ENTITY", "DIFFERENT_ENTITY"]), "CLEARED")
        self.assertEqual(alert_outcome(["DIFFERENT_ENTITY", "SAME_ENTITY"]), "ESCALATED")
        self.assertEqual(alert_outcome(["REVIEW"]), "NEEDS_ANALYST")
        self.assertEqual(alert_outcome([]), "NEEDS_ANALYST")

    def test_list_type_prefers_primary_category(self):
        self.assertEqual(list_type({"primary_category": "Sanctions", "categories": ["PEP"]}), "Sanctions")
        self.assertEqual(list_type({"primary_category": "", "categories": ["Adverse Media"]}), "Adverse Media")
        self.assertEqual(list_type({}), "Unspecified")

    def test_practice_metrics_count_only_labelled_rows(self):
        labels = [
            {"hit_id": "H-1", "decision": "SAME_ENTITY"},
            {"hit_id": "H-2", "decision": "REVIEW"},
            {"hit_id": "H-3", "decision": "DIFFERENT_ENTITY"},
        ]
        answers = {"H-1": "DIFFERENT_ENTITY", "H-2": "REVIEW", "H-3": "DIFFERENT_ENTITY"}
        metrics = practice_metrics(labels, answers)
        self.assertEqual(metrics["labelled"], 3)
        self.assertEqual(metrics["right"], 2)
        self.assertEqual(metrics["unsafe_clearances"], 1)
        self.assertLess(metrics["score"], 100.0)

    def test_summarise_separates_unlabelled_hits(self):
        predictions = [
            {"hit_id": "H-1", "alert_id": "A-1", "decision": "REVIEW", "reason_code": "INSUFFICIENT_DATA"},
            {"hit_id": "H-2", "alert_id": "A-1", "decision": "DIFFERENT_ENTITY", "reason_code": "LOCATION_MISMATCH"},
            {"hit_id": "H-3", "alert_id": "A-2", "decision": "SAME_ENTITY", "reason_code": "NAME_DOB_MATCH"},
        ]
        hits = {
            "H-1": {"primary_category": "PEP"},
            "H-2": {"primary_category": "Sanctions"},
            "H-3": {"categories": ["Adverse Media"]},
        }
        labels = [{"hit_id": "H-1", "decision": "REVIEW"}]
        report = summarise(predictions, hits, labels, {"H-1": "REVIEW"}, 300.0, "2026-11-21T17:00:00Z")
        self.assertEqual(report["hits"]["processed"], 3)
        self.assertEqual(report["hits"]["cleared"], 1)
        self.assertEqual(report["hits"]["confirmed"], 1)
        self.assertEqual(report["hits"]["review"], 1)
        self.assertEqual(report["alerts"]["processed"], 2)
        self.assertEqual(report["alerts"]["needs_analyst"], 1)
        self.assertEqual(report["alerts"]["escalated"], 1)
        self.assertEqual(report["unlabelled_hits"], 2)
        self.assertEqual(report["practice"]["labelled"], 1)
        self.assertEqual(report["practice"]["right"], 1)
        self.assertEqual(report["timing"]["ms_per_hit"], 100.0)
        self.assertEqual(report["timing"]["measured_at"], "2026-11-21T17:00:00Z")
        self.assertEqual(report["list_types"][0]["list_type"], "Sanctions")
        self.assertEqual(report["reason_codes"]["clearing"][0]["reason_code"], "LOCATION_MISMATCH")

    def test_timing_can_be_absent(self):
        report = summarise([], {}, [], {}, None, None)
        self.assertFalse(report["timing"]["measured"])
        self.assertIsNone(report["timing"]["ms_per_hit"])

    def test_name_only_baseline_never_clears(self):
        index = NameIndex([["muhammad", "mohammed"]], ["ltd"])
        same = name_only_decision(index, "Mohammed Saleh", {"name": "Muhammad Saleh", "aliases": [], "entity_type": "Individual"})
        review = name_only_decision(index, "Ada Costa", {"name": "Beatrice Rossi", "aliases": [], "entity_type": "Individual"})
        alias = name_only_decision(index, "Ada Costa", {"name": "Other", "aliases": ["Ada Costa"], "entity_type": "Individual"})
        self.assertEqual(same, "SAME_ENTITY")
        self.assertEqual(alias, "SAME_ENTITY")
        self.assertEqual(review, "REVIEW")
        labels = [
            {"hit_id": "H-1", "decision": "DIFFERENT_ENTITY"},
            {"hit_id": "H-2", "decision": "SAME_ENTITY"},
        ]
        metrics = practice_metrics(labels, {"H-1": "REVIEW", "H-2": "SAME_ENTITY"})
        self.assertEqual(metrics["unsafe_clearances"], 0)
        self.assertEqual(metrics["right"], 1)


class DevelopmentDashboardTests(unittest.TestCase):
    def test_real_development_numbers_when_the_folder_is_present(self):
        folder = os.environ.get("SCREEN_DATA", "/tmp/grokbot-data/dev")
        output = os.environ.get("SCREEN_OUTPUT", "/agent/repos/Cyber-hack-2026-grok/output/dev.jsonl")
        if not os.path.exists(os.path.join(folder, "practice_labels.jsonl")):
            self.skipTest("development data is not on this machine")
        import json
        from screen.engine import World

        def load(path):
            rows = []
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        rows.append(json.loads(line))
            return rows

        predictions = load(output)
        labels = load(os.path.join(folder, "practice_labels.jsonl"))
        hits = {row["hit_id"]: row for row in load(os.path.join(folder, "hits.jsonl"))}
        alerts = {row["alert_id"]: row for row in load(os.path.join(folder, "alerts.jsonl"))}
        world = World(folder)
        baseline = {}
        for row in predictions:
            if row["hit_id"] not in {item["hit_id"] for item in labels}:
                continue
            hit = hits[row["hit_id"]]
            screened = (alerts[row["alert_id"]].get("customer") or {}).get("name")
            baseline[row["hit_id"]] = name_only_decision(world.names, screened, hit)
        report = summarise(predictions, hits, labels, baseline, 1000.0, "measured-in-test")
        self.assertEqual(report["hits"]["processed"], 819)
        self.assertEqual(report["practice"]["labelled"], 50)
        self.assertEqual(report["practice"]["right"], 50)
        self.assertEqual(report["practice"]["unsafe_clearances"], 0)
        self.assertEqual(report["unlabelled_hits"], 769)
        self.assertEqual(report["name_only_baseline"]["labelled"], 50)
        self.assertEqual(report["name_only_baseline"]["unsafe_clearances"], 0)
        self.assertGreaterEqual(report["name_only_baseline"]["right"], 0)
        self.assertLessEqual(report["name_only_baseline"]["right"], 50)
        print(
            "NAME_ONLY_BASELINE",
            report["name_only_baseline"]["right"],
            "of",
            report["name_only_baseline"]["labelled"],
            "score",
            report["name_only_baseline"]["score"],
            "unsafe",
            report["name_only_baseline"]["unsafe_clearances"],
        )


if __name__ == "__main__":
    unittest.main()
