from datetime import date
from pathlib import Path
import unittest

from eu_grades_bot.drive import LocalWorkbookProvider
from eu_grades_bot.formatting import format_entries, format_period_entries_by_date, format_today_entries
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

    def test_period_formatter_groups_by_date_and_adds_summary(self):
        entries = [
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 11), "в", "example.xlsx", 4),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 11), "3", "example.xlsx", 4),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 12), "в", "example.xlsx", 5),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 12), "8", "example.xlsx", 5),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 13), "в", "example.xlsx", 6),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 13), "6", "example.xlsx", 6),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 14), "в", "example.xlsx", 7),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 14), "10", "example.xlsx", 7),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 15), "3", "example.xlsx", 8),
        ]

        message = format_period_entries_by_date(entries, empty_text="empty")

        self.assertIn(
            "11.05.2026\n"
            "🔴 Бази даних - відсутній\n"
            "🔴 Безпека інформаційних систем - 3",
            message,
        )
        self.assertIn(
            "15.05.2026\n"
            "🔴 Бази даних - 3",
            message,
        )
        self.assertIn(
            "Загальна кількість пропусків: 4 з 9. Пропущено 44% занять за 5 днів.",
            message,
        )
        self.assertIn("Загальна кількість оцінок: 5.", message)
        self.assertIn('Оцінки "відмінно": 1.', message)
        self.assertIn('Оцінки "добре": 1.', message)
        self.assertIn('Оцінки "задовільно": 1.', message)
        self.assertIn('Оцінки "незадовільно": 2.', message)

    def test_period_formatter_can_add_discipline_averages(self):
        entries = [
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 11), "в", "example.xlsx", 4),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 12), "3", "example.xlsx", 5),
            GradeEntry("Бази даних", "222", "Student", "student@example.com", date(2026, 5, 13), "8", "example.xlsx", 6),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 12), "10", "example.xlsx", 5),
            GradeEntry("Безпека інформаційних систем", "222", "Student", "student@example.com", date(2026, 5, 13), "9", "example.xlsx", 6),
            GradeEntry("Історія", "222", "Student", "student@example.com", date(2026, 5, 13), "в", "example.xlsx", 6),
        ]

        message = format_period_entries_by_date(
            entries,
            empty_text="empty",
            include_discipline_averages=True,
        )

        self.assertIn("Середній бал за дисциплінами:", message)
        self.assertIn("Бази даних - 5.5", message)
        self.assertIn("Безпека інформаційних систем - 9.5", message)
        self.assertIn("Історія - немає оцінок", message)


if __name__ == "__main__":
    unittest.main()
