"""The advisory service must fail closed and must not look like a live answer.

    PYTHONPATH=system python3 -m unittest screen.test_investigate
"""

from __future__ import annotations

import json
import os
import unittest
from urllib.error import HTTPError

from screen.investigate import investigate
from screen.test_adversarial import Case


class InvestigateTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("XAI_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["XAI_API_KEY"] = self._saved
        else:
            os.environ.pop("XAI_API_KEY", None)

    def test_missing_key_is_not_a_live_answer(self):
        case = Case()
        case.person("Ada Costa")
        _outcome, world = case.decide()
        result = investigate(world, "H-T001")
        self.assertFalse(result["live"])
        self.assertFalse(result["available"])
        self.assertEqual(result["source"], "unavailable")
        self.assertFalse(result["overrides_decision"])
        self.assertEqual(result["deterministic"]["decision"], "SAME_ENTITY")
        self.assertNotIn("grok", result["source"])

    def test_a_live_response_is_labelled_and_cannot_override(self):
        case = Case(name="Ada Costa", dob=None, country="Italy")
        case.person("Ada Costa", dob=None, residence="Italy")
        case.hit["dobs"] = []
        case.hit["countries"] = ["Japan"]
        _outcome, world = case.decide()

        def transport(model, packet, key):
            self.assertIn("KYC-T001", packet["allowed_evidence_ids"])
            return json.dumps({
                "assessment": "DIFFERENT_ENTITY",
                "supports": [],
                "contradicts": [{
                    "statement": "The customer is linked to Italy and the hit to Japan.",
                    "evidence_ids": ["KYC-T001"],
                }],
                "missing": ["No date of birth."],
                "next_checks": ["Ask the customer for a passport."],
                "uncertain": [],
            })

        os.environ["XAI_API_KEY"] = "test-key-not-real"
        result = investigate(world, "H-T001", transport=transport)
        self.assertTrue(result["live"])
        self.assertEqual(result["source"], "grok")
        self.assertFalse(result["overrides_decision"])
        self.assertEqual(result["deterministic"]["decision"], "DIFFERENT_ENTITY")
        self.assertEqual(result["assessment"], "DIFFERENT_ENTITY")
        self.assertEqual(result["contradicts"][0]["evidence_ids"], ["KYC-T001"])

    def test_provider_errors_leave_the_decision_in_place(self):
        case = Case()
        case.person("Ada Costa")
        _outcome, world = case.decide()
        os.environ["XAI_API_KEY"] = "test-key-not-real"

        def boom(model, packet, key):
            raise HTTPError("https://api.x.ai/v1/chat/completions", 429, "rate", hdrs=None, fp=None)

        result = investigate(world, "H-T001", transport=boom)
        self.assertFalse(result["live"])
        self.assertEqual(result["error"], "rate_limited")
        self.assertEqual(result["deterministic"]["decision"], "SAME_ENTITY")
        self.assertFalse(result["overrides_decision"])

    def test_malformed_model_json_is_rejected(self):
        case = Case()
        case.person("Ada Costa")
        _outcome, world = case.decide()
        os.environ["XAI_API_KEY"] = "test-key-not-real"
        result = investigate(world, "H-T001", transport=lambda model, packet, key: "not-json")
        self.assertEqual(result["error"], "malformed")
        self.assertFalse(result["overrides_decision"])
        self.assertEqual(result["deterministic"]["decision"], "SAME_ENTITY")


if __name__ == "__main__":
    unittest.main()
