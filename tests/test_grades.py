from datetime import date
from pathlib import Path
import unittest

from eu_grades_bot.drive import LocalWorkbookProvider
from eu_grades_bot.formatting import format_entries, format_today_entries
from eu_grades_bot.grades import GradeEntry, GradesRepository, discipline_from_title


ROOT = Path(__file__).resolve().parents[1]


class GradesRepositoryTest(unittest.TestCase):
    def test_discipline_from_title(self):
        self.assertEqual(
            discipline_from_title("Бази даних - Чопоров С.В."),
            "Бази даних",
        )

    def test_reads_student_entries_from_examples(self):
        repository = GradesRepository(LocalWorkbookProvider(ROOT / "examples"), cache_ttl_seconds=3600)
        grades = repository.get_student_grades("kovalenko.o@e-u.edu.ua")

        self.assertEqual(grades.student_name, "Коваленко Олександр Вікторович")
        self.assertEqual(grades.disciplines, ("Бази даних", "Безпека інформаційних систем"))
        self.assertEqual(len(grades.entries), 10)
        self.assertTrue(any(entry.value == "в" for entry in grades.entries))
        self.assertTrue(any(entry.value == "3" for entry in grades.entries))

    def test_formatter_marks_low_and_high_values(self):
        entries = [
            GradeEntry(
                discipline="Бази даних",
                group="222",
                student_name="Student",
                email="student@example.com",
                date=date(2026, 5, 12),
                value="11",
                source_file="example.xlsx",
                column_index=4,
            ),
            GradeEntry(
                discipline="Бази даних",
                group="222",
                student_name="Student",
                email="student@example.com",
                date=date(2026, 5, 14),
                value="в",
                source_file="example.xlsx",
                column_index=5,
            ),
        ]
        message = format_entries(entries, empty_text="empty")

        self.assertIn("<b>Бази даних</b>", message)
        self.assertIn("🟢 12.05.2026 - 11", message)
        self.assertIn("🔴 14.05.2026 - відсутній", message)
        self.assertNotIn(" - в\n", f"{message}\n")

    def test_today_formatter_groups_grades_and_absences_without_repeating_dates(self):
        entries = [
            GradeEntry(
                discipline="Бази даних",
                group="222",
                student_name="Student",
                email="student@example.com",
                date=date(2026, 5, 12),
                value="11",
                source_file="example.xlsx",
                column_index=4,
            ),
            GradeEntry(
                discipline="Бази даних",
                group="222",
                student_name="Student",
                email="student@example.com",
                date=date(2026, 5, 12),
                value="в",
                source_file="example.xlsx",
                column_index=5,
            ),
            GradeEntry(
                discipline="Безпека інформаційних систем",
                group="222",
                student_name="Student",
                email="student@example.com",
                date=date(2026, 5, 12),
                value="2",
                source_file="example.xlsx",
                column_index=6,
            ),
        ]

        message = format_today_entries(
            entries,
            ("Бази даних", "Безпека інформаційних систем"),
            date(2026, 5, 12),
        )

        self.assertEqual(message.count("12.05.2026"), 1)
        self.assertNotIn("12.05.2026 -", message)
        self.assertIn("Оцінки:\nБази даних - 11\nБезпека інформаційних систем - 2", message)
        self.assertIn("Пропуски:\nБази даних\nЗагальна кількість пропусків: 1", message)
        self.assertNotIn("Бази даних - відсутній", message)
        self.assertIn("Загальна кількість пропусків: 1", message)

    def test_today_formatter_prints_subjects_when_no_data(self):
        message = format_today_entries(
            [],
            ("Бази даних", "Безпека інформаційних систем"),
            date(2026, 5, 12),
        )

        self.assertEqual(
            message,
            "12.05.2026\n\n"
            "Бази даних\n"
            "Безпека інформаційних систем\n"
            "Не має даних щодо пропусків та оцінок",
        )


if __name__ == "__main__":
    unittest.main()
