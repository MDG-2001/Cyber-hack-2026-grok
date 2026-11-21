"""Empty and placeholder screening names are refused before the engine runs."""

from __future__ import annotations

import unittest

from screen.intake import screen_name_error


class IntakeTests(unittest.TestCase):
    def test_blank_and_placeholders_are_empty(self):
        for name in ("", "   ", "NA", "N/A", "TEST", "UNKNOWN", "XXX", "NONE", "12345", "NA TEST"):
            self.assertIsNotNone(screen_name_error(name), name)

    def test_a_real_name_is_accepted(self):
        self.assertIsNone(screen_name_error("Ada Costa"))
        self.assertIsNone(screen_name_error("NA Costa"))


if __name__ == "__main__":
    unittest.main()
