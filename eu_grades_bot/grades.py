from __future__ import annotations

import re
import time
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Protocol

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .drive import WorkbookSource


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADERLESS_NAME_COLUMN = 2
HEADERLESS_EMAIL_COLUMN = 3
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GradeEntry:
    discipline: str
    group: str
    student_name: str
    email: str
    date: date
    value: str
    source_file: str
    column_index: int


@dataclass(frozen=True)
class StudentRecord:
    discipline: str
    student_name: str
    email: str
    entries: tuple[GradeEntry, ...]


@dataclass(frozen=True)
class StudentGrades:
    email: str
    student_name: str | None
    disciplines: tuple[str, ...]
    entries: tuple[GradeEntry, ...]


class WorkbookProvider(Protocol):
    def list_workbooks(self) -> list[WorkbookSource]:
        ...


class GradesRepository:
    def __init__(
        self,
        provider: WorkbookProvider,
        cache_ttl_seconds: int = 300,
        reload_when_stale: bool = True,
    ):
        self.provider = provider
        self.cache_ttl_seconds = cache_ttl_seconds
        self.reload_when_stale = reload_when_stale
        self._loaded_at = 0.0
        self._entries_by_email: dict[str, list[GradeEntry]] = {}
        self._names_by_email: dict[str, str] = {}
        self._disciplines_by_email: dict[str, tuple[str, ...]] = {}

    def get_student_grades(self, email: str, force_reload: bool = False) -> StudentGrades:
        normalized_email = normalize_email(email)
        self._ensure_loaded(force_reload=force_reload)
        entries = tuple(sorted(self._entries_by_email.get(normalized_email, []), key=_entry_sort_key))
        return StudentGrades(
            email=normalized_email,
            student_name=self._names_by_email.get(normalized_email),
            disciplines=self._disciplines_by_email.get(normalized_email, ()),
            entries=entries,
        )

    def _ensure_loaded(self, force_reload: bool = False) -> None:
        if (
            not force_reload
            and self._loaded_at > 0
            and (
                not self.reload_when_stale
                or time.monotonic() - self._loaded_at < self.cache_ttl_seconds
            )
        ):
            return
        self.reload()

    def reload(self) -> None:
        entries_by_email: dict[str, list[GradeEntry]] = defaultdict(list)
        names_by_email: dict[str, str] = {}
        disciplines_by_email: dict[str, list[str]] = defaultdict(list)

        sources = self.provider.list_workbooks()
        logger.info("Loading grades from %d workbook(s).", len(sources))
        total_entries = 0
        for source in sources:
            discipline = discipline_from_title(source.title)
            source_entries = 0
            for student in iter_workbook_students(source.path, discipline, source.title):
                entries_by_email[student.email].extend(student.entries)
                names_by_email.setdefault(student.email, student.student_name)
                if discipline not in disciplines_by_email[student.email]:
                    disciplines_by_email[student.email].append(discipline)
                source_entries += len(student.entries)
            total_entries += source_entries
            logger.info("Workbook %s produced %d grade/absence record(s).", source.title, source_entries)

        self._entries_by_email = dict(entries_by_email)
        self._names_by_email = names_by_email
        self._disciplines_by_email = {
            email: tuple(disciplines)
            for email, disciplines in disciplines_by_email.items()
        }
        self._loaded_at = time.monotonic()
        logger.info(
            "Loaded %d grade/absence record(s) for %d email(s).",
            total_entries,
            len(self._names_by_email),
        )


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(normalize_email(email)))


def discipline_from_title(title: str) -> str:
    stem = Path(title).stem
    if " - " in stem:
        return stem.rsplit(" - ", 1)[0].strip()
    return stem.strip()


def iter_workbook_entries(path: Path, discipline: str, source_title: str | None = None) -> Iterable[GradeEntry]:
    for student in iter_workbook_students(path, discipline, source_title):
        yield from student.entries


def iter_workbook_students(
    path: Path,
    discipline: str,
    source_title: str | None = None,
) -> Iterable[StudentRecord]:
    workbook = load_workbook(path, data_only=True, read_only=False)
    try:
        for sheet in workbook.worksheets:
            yield from iter_sheet_students(sheet, discipline, source_title or path.name)
    finally:
        workbook.close()


def iter_sheet_entries(sheet: Worksheet, discipline: str, source_file: str) -> Iterable[GradeEntry]:
    for student in iter_sheet_students(sheet, discipline, source_file):
        yield from student.entries


def iter_sheet_students(sheet: Worksheet, discipline: str, source_file: str) -> Iterable[StudentRecord]:
    header_row = _find_header_row(sheet)
    if header_row is None:
        # Some exported grade books have no text headers: the first row contains
        # dates, while student names and emails are stored in columns 2 and 3.
        header_row = _find_date_header_row(sheet)
        if header_row is None:
            return
        email_col = HEADERLESS_EMAIL_COLUMN
        name_col = HEADERLESS_NAME_COLUMN
    else:
        email_col = _find_email_column(sheet, header_row)
        if email_col is None:
            return
        name_col = _find_name_column(sheet, header_row)

    date_columns = _find_date_columns(sheet, header_row)
    if not date_columns:
        return

    for row_index in range(header_row + 1, sheet.max_row + 1):
        raw_email = sheet.cell(row=row_index, column=email_col).value
        if raw_email is None:
            continue
        email = normalize_email(str(raw_email))
        if not is_valid_email(email):
            continue

        raw_name = sheet.cell(row=row_index, column=name_col).value if name_col else None
        student_name = str(raw_name).strip() if raw_name else ""

        entries: list[GradeEntry] = []
        for column_index, entry_date in date_columns:
            raw_value = sheet.cell(row=row_index, column=column_index).value
            value = normalize_grade_value(raw_value)
            if value is None:
                continue
            entries.append(
                GradeEntry(
                    discipline=discipline,
                    group=sheet.title,
                    student_name=student_name,
                    email=email,
                    date=entry_date,
                    value=value,
                    source_file=source_file,
                    column_index=column_index,
                )
            )
        yield StudentRecord(
            discipline=discipline,
            student_name=student_name,
            email=email,
            entries=tuple(entries),
        )


def normalize_grade_value(raw_value) -> str | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return None
        if value.lower() == "в":
            return "в"
        return value
    if isinstance(raw_value, float) and raw_value.is_integer():
        return str(int(raw_value))
    if isinstance(raw_value, int):
        return str(raw_value)
    return str(raw_value).strip() or None


def is_absence(value: str) -> bool:
    return value.strip().lower() == "в"


def numeric_grade(value: str) -> int | None:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None
    if not number.is_integer():
        return None
    return int(number)


def _find_header_row(sheet: Worksheet) -> int | None:
    for row_index in range(1, min(sheet.max_row, 10) + 1):
        if _find_email_column(sheet, row_index) is not None:
            return row_index
    return None


def _find_date_header_row(sheet: Worksheet) -> int | None:
    for row_index in range(1, min(sheet.max_row, 10) + 1):
        if _find_date_columns(sheet, row_index):
            return row_index
    return None


def _find_email_column(sheet: Worksheet, row_index: int) -> int | None:
    for column_index in range(1, sheet.max_column + 1):
        value = sheet.cell(row=row_index, column=column_index).value
        text = str(value).strip().lower() if value is not None else ""
        if "електрон" in text or "email" in text or "e-mail" in text:
            return column_index
    return None


def _find_name_column(sheet: Worksheet, row_index: int) -> int | None:
    for column_index in range(1, sheet.max_column + 1):
        value = sheet.cell(row=row_index, column=column_index).value
        text = str(value).strip().lower() if value is not None else ""
        if "прізвище" in text or "піб" in text or "студент" in text:
            return column_index
    return None


def _find_date_columns(sheet: Worksheet, row_index: int) -> list[tuple[int, date]]:
    result: list[tuple[int, date]] = []
    for column_index in range(1, sheet.max_column + 1):
        value = sheet.cell(row=row_index, column=column_index).value
        parsed = parse_date_cell(value)
        if parsed is not None:
            result.append((column_index, parsed))
    return result


def parse_date_cell(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _entry_sort_key(entry: GradeEntry):
    return (entry.discipline.casefold(), entry.date, entry.column_index, entry.group.casefold())
