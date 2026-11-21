"""Ages have to belong to the same person and the same event.

Run from the repository root:

    PYTHONPATH=system python3 -m unittest screen.test_births
"""

from __future__ import annotations

import os
import unittest

from screen.dates import parse_when
from screen.engine import age_births, resolve_dob, shared_reason_code


FABIO = (
    "Fabio Pinna, 24, a politically exposed person in Italy, "
    "was included in a public report in September 1992."
)


class AgeBirthsTests(unittest.TestCase):
    def test_explicit_age_and_date_in_one_sentence(self):
        text = "In March 2019, a report described a 45-year-old analyst."
        found = age_births(text, "notes")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].years, (1973, 1974))
        self.assertTrue(found[0].confident)

    def test_pronoun_was_is_kept_when_the_date_is_in_the_same_sentence(self):
        text = "Investigators questioned Alexander Ostrowsky in April 2006, when he was 42."
        found = age_births(text, "notes")
        self.assertEqual(found[0].years, (1963, 1964))
        self.assertTrue(found[0].confident)

    def test_name_was_n_when_is_the_same_event(self):
        text = "Frank Murud was 48 when he was named in an indictment in October 2004."
        found = age_births(text, "notes")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].years, (1955, 1956))
        self.assertTrue(found[0].confident)

    def test_at_the_age_of_is_not_confused_with_a_month(self):
        text = "During December 2012, Jean-Philippe Laurent faced charges while at the age of 43."
        found = age_births(text, "notes")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].confident)
        self.assertEqual(found[0].years, (1968, 1969))

    def test_named_person_was_n_in_a_dated_sentence(self):
        text = "In June 2006, when questioning by investigators took place, Tomasz Drozd was 27."
        found = age_births(text, "notes")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].confident)
        self.assertEqual(found[0].years, (1978, 1979))

    def test_bare_was_n_is_not_an_age(self):
        text = "The file was 24 pages long in March 1992."
        self.assertEqual(age_births(text, "search"), [])

    def test_unrelated_age_and_date_in_different_sentences(self):
        text = "John Smith was 40. A different company filed its accounts in March 1990."
        self.assertEqual(age_births(text, "search"), [])

    def test_unrelated_date_is_not_paired_inside_one_sentence(self):
        text = "John Smith, 40, met Mary Jones, who was born in March 1980, at the conference."
        self.assertEqual(age_births(text, "notes"), [])

    def test_multiple_people_with_different_ages(self):
        text = (
            "John Smith, 40, and Mary Jones, 33, were both mentioned "
            "in a report from March 1990."
        )
        self.assertEqual(age_births(text, "search"), [])

    def test_second_person_between_age_and_date(self):
        text = "John Smith, 40, met Mary Jones in March 1990."
        self.assertEqual(age_births(text, "search"), [])

    def test_conflicting_years_in_one_sentence(self):
        text = "Aged 45 in March 2019 and again in March 2010, the notice was unclear."
        self.assertEqual(age_births(text, "notes"), [])

    def test_conflicting_years_across_sentences(self):
        text = "Aged 45 in March 2019. Aged 50 in March 2010."
        self.assertEqual(age_births(text, "notes"), [])

    def test_missing_evidence(self):
        self.assertEqual(age_births("", "search"), [])
        self.assertEqual(age_births("No dates and no ages here.", "notes"), [])
        self.assertEqual(age_births("Aged 45, with no event date.", "notes"), [])

    def test_h_2ba2db_comma_age_is_not_confident(self):
        # The age and the report date are in different clauses of one sentence.
        found = age_births(FABIO, "search")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].years, (1967, 1968))
        self.assertFalse(found[0].confident)

    def test_low_confidence_age_cannot_be_the_only_mismatch(self):
        customer = parse_when("1976-09-05")
        births = age_births(FABIO, "search")
        agrees, mismatch = resolve_dob(customer, births)
        self.assertFalse(agrees)
        self.assertFalse(mismatch)

    def test_low_confidence_age_can_still_agree(self):
        customer = parse_when("1968-03-05")
        births = age_births(FABIO, "search")
        agrees, mismatch = resolve_dob(customer, births)
        self.assertTrue(agrees)
        self.assertFalse(mismatch)

    def test_confident_mismatch_still_clears(self):
        text = "A regulator's notice from May 2002 listed Aleksandar Raković, 29, a politically exposed person in Serbia."
        found = age_births(text, "search")
        self.assertTrue(found[0].confident)
        self.assertEqual(found[0].years, (1972, 1973))
        agrees, mismatch = resolve_dob(parse_when("1979-04-17"), found)
        self.assertFalse(agrees)
        self.assertTrue(mismatch)

    def test_close_comma_age_stays_confident(self):
        text = "In May 2013, Farah Damji, 46, named in adverse media in United Kingdom, was named in a public report."
        found = age_births(text, "search")
        self.assertTrue(found and found[0].confident)


class SharedCodeTests(unittest.TestCase):
    def test_code_must_fit_every_customer(self):
        # H-AF6CD2 shape: one customer mismatches the name, both mismatch the country.
        per_party = [
            {"NAME_MISMATCH_VERIFIED", "LOCATION_MISMATCH"},
            {"LOCATION_MISMATCH"},
        ]
        self.assertEqual(shared_reason_code(per_party), "LOCATION_MISMATCH")

    def test_empty_intersection_does_not_invent_a_code(self):
        per_party = [{"NAME_MISMATCH_VERIFIED"}, {"LOCATION_MISMATCH"}]
        self.assertIsNone(shared_reason_code(per_party))

    def test_single_customer_keeps_judge_priority(self):
        codes = {"DOB_MISMATCH", "LOCATION_MISMATCH", "NAME_MISMATCH_VERIFIED"}
        self.assertEqual(shared_reason_code([codes]), "DOB_MISMATCH")


class Hit2BA2DBTests(unittest.TestCase):
    def test_development_hit_is_not_cleared_on_the_loose_age(self):
        folder = os.environ.get("SCREEN_DATA", "/tmp/grokbot-data/dev")
        hits = os.path.join(folder, "hits.jsonl")
        if not os.path.exists(hits):
            self.skipTest("development data is not on this machine")
        from screen.engine import World

        world = World(folder)
        hit = world.hits["H-2BA2DB"]
        outcome = world.decide_hit(world.alerts[hit["alert_id"]], hit)
        self.assertNotEqual(outcome.reason_code, "DOB_MISMATCH")
        self.assertEqual(outcome.decision, "REVIEW")
        self.assertNotIn("1967 or 1968", outcome.reason)


if __name__ == "__main__":
    unittest.main()
