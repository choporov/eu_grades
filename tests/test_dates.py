from datetime import date
import unittest

from eu_grades_bot.dates import current_week_bounds, previous_week_bounds


class DatesTest(unittest.TestCase):
    def test_current_week_bounds(self):
        self.assertEqual(
            current_week_bounds(date(2026, 5, 12)),
            (date(2026, 5, 11), date(2026, 5, 17)),
        )

    def test_previous_week_bounds(self):
        self.assertEqual(
            previous_week_bounds(date(2026, 5, 12)),
            (date(2026, 5, 4), date(2026, 5, 10)),
        )


if __name__ == "__main__":
    unittest.main()

