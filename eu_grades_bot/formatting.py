from __future__ import annotations

import html
from collections import defaultdict
from datetime import date

from .grades import GradeEntry, is_absence, numeric_grade


LOW_MARKER = "🔴"
HIGH_MARKER = "🟢"
DEFAULT_MARKER = "•"
MAX_TELEGRAM_MESSAGE_LENGTH = 3900


def format_subjects(disciplines: tuple[str, ...]) -> str:
    if not disciplines:
        return "За цією email-адресою дисципліни не знайдено."
    lines = ["Знайдено дисципліни:"]
    lines.extend(f"• {html.escape(discipline)}" for discipline in disciplines)
    return "\n".join(lines)


def format_entries(entries: list[GradeEntry] | tuple[GradeEntry, ...], empty_text: str) -> str:
    if not entries:
        return empty_text

    grouped: dict[str, list[GradeEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.discipline].append(entry)

    parts: list[str] = []
    for discipline, discipline_entries in grouped.items():
        parts.append(f"<b>{html.escape(discipline)}</b>")
        for entry in sorted(discipline_entries, key=lambda e: (e.date, e.column_index, e.group.casefold())):
            parts.append(format_entry_line(entry))
        parts.append("")
    return "\n".join(parts).strip()


def format_period_entries_by_date(
    entries: list[GradeEntry] | tuple[GradeEntry, ...],
    empty_text: str,
) -> str:
    if not entries:
        return empty_text

    grouped: dict[date, list[GradeEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.date].append(entry)

    parts: list[str] = []
    for day in sorted(grouped):
        parts.append(format_date(day))
        for entry in sorted(grouped[day], key=lambda e: (e.discipline.casefold(), e.column_index, e.group.casefold())):
            parts.append(format_period_entry_line(entry))
        parts.append("")

    parts.append(format_period_summary(entries))
    return "\n".join(parts).strip()


def format_period_entry_line(entry: GradeEntry) -> str:
    marker = marker_for_value(entry.value)
    return f"{marker} {html.escape(entry.discipline)} - {html.escape(display_value(entry.value))}"


def format_period_summary(entries: list[GradeEntry] | tuple[GradeEntry, ...]) -> str:
    total_lessons = len(entries)
    absence_count = sum(1 for entry in entries if is_absence(entry.value))
    days_count = len({entry.date for entry in entries})
    absence_percent = round(absence_count / total_lessons * 100) if total_lessons else 0
    grades = [grade for entry in entries if (grade := numeric_grade(entry.value)) is not None]
    excellent_count = sum(1 for grade in grades if grade > 9)
    good_count = sum(1 for grade in grades if 7 <= grade <= 9)
    satisfactory_count = sum(1 for grade in grades if 5 <= grade <= 7)
    unsatisfactory_count = sum(1 for grade in grades if grade < 5)

    return "\n".join(
        [
            f"Загальна кількість пропусків: {absence_count} з {total_lessons}. "
            f"Пропущено {absence_percent}% занять за {days_count} {day_word(days_count)}.",
            f"Загальна кількість оцінок: {len(grades)}.",
            f'Оцінки "відмінно": {excellent_count}.',
            f'Оцінки "добре": {good_count}.',
            f'Оцінки "задовільно": {satisfactory_count}.',
            f'Оцінки "незадовільно": {unsatisfactory_count}.',
        ]
    )


def day_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "дні"
    return "днів"


def format_today_entries(
    entries: list[GradeEntry] | tuple[GradeEntry, ...],
    disciplines: tuple[str, ...],
    day: date,
) -> str:
    grades = [entry for entry in entries if not is_absence(entry.value)]
    absences = [entry for entry in entries if is_absence(entry.value)]
    subject_list = format_subject_list(disciplines)

    parts: list[str] = [format_date(day), ""]
    if not grades and not absences:
        parts.extend([subject_list, "Не має даних щодо пропусків та оцінок"])
        return "\n".join(parts).strip()

    if grades:
        parts.append("Оцінки:")
        parts.extend(format_today_entry_line(entry) for entry in sort_today_entries(grades))
    else:
        parts.extend([subject_list, "Оцінки відсутні"])

    parts.append("")
    if absences:
        parts.append("Пропуски:")
        parts.extend(format_today_absence_line(entry) for entry in sort_today_entries(absences))
        parts.append(f"Загальна кількість пропусків: {len(absences)}")
    else:
        parts.extend([subject_list, "Пропуски відсутні"])

    return "\n".join(parts).strip()


def format_subject_list(disciplines: tuple[str, ...]) -> str:
    if not disciplines:
        return "Дисципліни не знайдено."
    return "\n".join(html.escape(discipline) for discipline in disciplines)


def sort_today_entries(entries: list[GradeEntry]) -> list[GradeEntry]:
    return sorted(entries, key=lambda e: (e.discipline.casefold(), e.column_index, e.group.casefold()))


def format_today_entry_line(entry: GradeEntry) -> str:
    return f"{html.escape(entry.discipline)} - {html.escape(display_value(entry.value))}"


def format_today_absence_line(entry: GradeEntry) -> str:
    return html.escape(entry.discipline)


def format_entry_line(entry: GradeEntry) -> str:
    marker = marker_for_value(entry.value)
    return f"{marker} {format_date(entry.date)} - {html.escape(display_value(entry.value))}"


def display_value(value: str) -> str:
    if is_absence(value):
        return "відсутній"
    return value


def marker_for_value(value: str) -> str:
    if is_absence(value):
        return LOW_MARKER
    grade = numeric_grade(value)
    if grade is None:
        return DEFAULT_MARKER
    if 1 <= grade <= 3:
        return LOW_MARKER
    if 10 <= grade <= 12:
        return HIGH_MARKER
    return DEFAULT_MARKER


def format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def split_telegram_message(text: str) -> list[str]:
    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in text.split("\n\n"):
        paragraph_length = len(paragraph) + 2
        if current and current_length + paragraph_length > MAX_TELEGRAM_MESSAGE_LENGTH:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_length = paragraph_length
        else:
            current.append(paragraph)
            current_length += paragraph_length
    if current:
        chunks.append("\n\n".join(current))
    return chunks
