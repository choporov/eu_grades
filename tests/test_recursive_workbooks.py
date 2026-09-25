from datetime import date
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook

from eu_grades_bot.drive import (
    DriveWorkbookProvider,
    GOOGLE_SHEETS_MIME,
    GOOGLE_SHORTCUT_MIME,
    LocalWorkbookProvider,
    XLSX_MIME,
)
from eu_grades_bot.grades import GradesRepository
from eu_grades_bot.update_log import UpdateLog


FOLDER_MIME = "application/vnd.google-apps.folder"
WORKBOOK_NAME = "Біологія - Викладач.xlsx"


def workbook_bytes(value=10):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Група"
    sheet.append([None, "Група", None, date(2026, 9, 1)])
    sheet.append([None, "Підгрупа", None, 12])
    sheet.append([1, "Студент", "student@example.com", value])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def folder_info(folder_id, name):
    return {"id": folder_id, "name": name, "mimeType": FOLDER_MIME}


def file_info(file_id, name=WORKBOOK_NAME, mime_type=XLSX_MIME):
    return {"id": file_id, "name": name, "mimeType": mime_type}


class RecursiveLocalWorkbooksTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_workbook(self, relative_path, value=10):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(workbook_bytes(value))
        return path

    def test_reads_multiple_levels_and_same_names_as_separate_workbooks(self):
        expected = {
            self.write_workbook(WORKBOOK_NAME, 7),
            self.write_workbook(f"Курс 1/{WORKBOOK_NAME}", 8),
            self.write_workbook(f"Курс 2/Семестр/{WORKBOOK_NAME}", 9),
            self.write_workbook("Курс 2/Семестр/Математика - Викладач.xlsm", 10),
            self.write_workbook("Курс 2/_Окремий файл.xlsx", 11),
        }
        self.write_workbook(f"Курс 1/~${WORKBOOK_NAME}", 12)
        (self.root / "notes.txt").write_text("not a workbook", encoding="utf-8")
        provider = LocalWorkbookProvider(self.root)

        self.assertEqual({source.path for source in provider.list_workbooks()}, expected)
        grades = GradesRepository(provider).get_student_grades("student@example.com")
        self.assertEqual(len(grades.entries), 5)
        self.assertEqual(sorted(entry.value for entry in grades.entries), ["10", "11", "7", "8", "9"])

    def test_excluded_directories_are_never_opened(self):
        included = self.write_workbook(f"Курс/Семестр/{WORKBOOK_NAME}")
        self.write_workbook(f"_Архів/Вкладена/{WORKBOOK_NAME}")
        self.write_workbook(f"Курс/_Чернетки/Ще глибше/{WORKBOOK_NAME}")
        # An ignored shortcut must not require credentials or be exported.
        (self.root / "_Архів" / "table.gsheet").write_text("invalid metadata", encoding="utf-8")
        actual_iterdir = Path.iterdir
        visited = []

        def checked_iterdir(path):
            visited.append(path)
            self.assertFalse(any(part.startswith("_") for part in path.relative_to(self.root).parts))
            return actual_iterdir(path)

        with patch.object(Path, "iterdir", checked_iterdir):
            sources = LocalWorkbookProvider(self.root).list_workbooks()

        self.assertEqual([source.path for source in sources], [included])
        self.assertIn(included.parent, visited)

    def test_excluded_root_is_not_opened_including_dot_path(self):
        root = self.root / "_Архів"
        self.write_workbook(f"_Архів/{WORKBOOK_NAME}")
        self.write_workbook(f"_Архів/Вкладена/{WORKBOOK_NAME}")
        with patch.object(Path, "iterdir", side_effect=AssertionError("Excluded root was opened")):
            self.assertEqual(LocalWorkbookProvider(root).list_workbooks(), [])
            with patch("pathlib.os.getcwd", return_value=str(root)):
                self.assertEqual(LocalWorkbookProvider(Path(".")).list_workbooks(), [])

    def test_nested_gsheet_exports_do_not_reenter_cache_on_next_refresh(self):
        for use_custom_cache in (False, True):
            with self.subTest(custom_cache=use_custom_cache):
                root = self.root / str(use_custom_cache)
                for directory, sheet_id in (("Курс 1", "sheet-one"), ("Курс 2/Семестр", "sheet-two")):
                    shortcut = root / directory / "Біологія - Викладач.gsheet"
                    shortcut.parent.mkdir(parents=True, exist_ok=True)
                    shortcut.write_text(json.dumps({"doc_id": sheet_id}), encoding="utf-8")
                provider = LocalWorkbookProvider(
                    root,
                    credentials_file=self.root / "unused.json",
                    cache_dir=root / "generated/cache" if use_custom_cache else None,
                )
                service = Mock()
                service.files.return_value.export_media.side_effect = lambda fileId, mimeType: fileId
                payloads = {"sheet-one": workbook_bytes(8), "sheet-two": workbook_bytes(11)}
                with patch("eu_grades_bot.drive.build_drive_service", return_value=service), patch(
                    "eu_grades_bot.drive.download_media",
                    side_effect=lambda request, target: target.write_bytes(payloads[request]),
                ):
                    first = provider.list_workbooks()
                    second = provider.list_workbooks()
                    repository = GradesRepository(provider)
                    repository.reload()
                self.assertEqual(len(first), 2)
                self.assertEqual(len(second), 2)
                self.assertEqual(len({source.path for source in second}), 2)
                self.assertEqual(len(repository.get_cached_student_grades("student@example.com").entries), 2)
                self.assertEqual(service.files.return_value.export_media.call_count, 6)

    def test_directory_symlinks_are_not_followed(self):
        included = self.write_workbook(f"Курс/{WORKBOOK_NAME}")
        hidden = self.write_workbook(f"_Архів/{WORKBOOK_NAME}")
        try:
            (self.root / "loop").symlink_to(self.root, target_is_directory=True)
            (self.root / "alias").symlink_to(hidden.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks are unavailable: {exc}")
        sources = LocalWorkbookProvider(self.root).list_workbooks()
        self.assertEqual([source.path for source in sources], [included])

    def test_directory_read_error_preserves_previous_snapshot(self):
        path = self.write_workbook(f"Курс/{WORKBOOK_NAME}")
        repository = GradesRepository(LocalWorkbookProvider(self.root))
        repository.reload()
        previous_time = repository.cache_info()[0]
        actual_iterdir = Path.iterdir

        def failing_iterdir(folder):
            if folder == path.parent:
                raise PermissionError("Cannot read nested directory")
            return actual_iterdir(folder)

        with patch.object(Path, "iterdir", failing_iterdir):
            with self.assertRaises(PermissionError):
                repository.reload()
        self.assertEqual(repository.cache_info()[0], previous_time)
        self.assertEqual(len(repository.get_cached_student_grades("student@example.com").entries), 1)


class RecursiveDriveWorkbooksTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root_name = "Журнали"
        self.pages = {("root", None): {"files": []}}
        self.payloads = {}
        self.service = Mock()
        self.resource = self.service.files.return_value
        self.resource.get.return_value.execute.side_effect = lambda: {"name": self.root_name}
        self.resource.list.side_effect = self.list_children
        self.resource.get_media.side_effect = lambda fileId: fileId
        self.resource.export_media.side_effect = lambda fileId, mimeType: fileId
        self.log = UpdateLog(self.root / "update.log")
        self.provider = DriveWorkbookProvider(
            "root", self.root / "unused.json", self.root / "cache", update_log=self.log,
        )
        self.provider._build_service = lambda: self.service
        self.provider._download = Mock(side_effect=lambda request, target: target.write_bytes(self.payloads[request]))

    def list_children(self, **kwargs):
        self.assertNotIn("mimeType !=", kwargs["q"])
        self.assertTrue(kwargs["supportsAllDrives"])
        self.assertTrue(kwargs["includeItemsFromAllDrives"])
        folder_id = kwargs["q"].split("'")[1]
        response = self.pages[(folder_id, kwargs["pageToken"])]
        if isinstance(response, Exception):
            raise response
        return Mock(execute=Mock(return_value=response))

    def listed_folders(self):
        return [call.kwargs["q"].split("'")[1] for call in self.resource.list.call_args_list]

    def log_records(self):
        return [json.loads(line) for line in self.log.path.read_text(encoding="utf-8").splitlines()]

    def test_recursive_paginated_listing_preserves_formats_and_duplicate_names(self):
        self.pages = {
            ("root", None): {"files": [file_info("first"), folder_info("course", "Курс")], "nextPageToken": "root-page-2"},
            ("root", "root-page-2"): {"files": [folder_info("archive", "_Архів"), file_info("underscore", "_Окремий файл.xlsx")]},
            ("course", None): {"files": [file_info("second"), folder_info("semester", "Семестр")], "nextPageToken": "course-page-2"},
            ("course", "course-page-2"): {"files": [folder_info("drafts", "_Чернетки")]},
            ("semester", None): {"files": [
                file_info("native", "Хімія - Викладач", GOOGLE_SHEETS_MIME),
                {**file_info("shortcut", "Фізика - Викладач", GOOGLE_SHORTCUT_MIME), "shortcutDetails": {
                    "targetId": "physics", "targetMimeType": GOOGLE_SHEETS_MIME,
                }},
                file_info("metadata", "Математика - Викладач.gsheet", "application/json"),
            ]},
        }
        self.payloads = {key: workbook_bytes(value) for key, value in (
            ("first", 7), ("second", 8), ("underscore", 9), ("native", 10), ("physics", 11), ("math", 12),
        )}
        self.payloads["metadata"] = b'{"doc_id": "math"}'
        repository = GradesRepository(self.provider, update_log=self.log)
        repository.reload()

        grades = repository.get_cached_student_grades("student@example.com")
        self.assertEqual(len(grades.entries), 6)
        self.assertEqual({entry.value for entry in grades.entries}, {"7", "8", "9", "10", "11", "12"})
        self.assertEqual(self.listed_folders(), ["root", "root", "course", "course", "semester"])
        self.resource.get.assert_called_once_with(fileId="root", fields="name", supportsAllDrives=True)
        self.assertEqual(len({record["cache_file"] for record in self.log_records()}), 6)
        self.assertEqual(len([record for record in self.log_records() if record["event"] == "parse_result"]), 6)
        self.assertEqual({call.args[0] for call in self.provider._download.call_args_list}, set(self.payloads))

    def test_excluded_root_is_checked_again_on_every_refresh(self):
        self.pages[("root", None)] = {"files": [file_info("first")]}
        self.payloads["first"] = workbook_bytes()
        repository = GradesRepository(self.provider, update_log=self.log)
        repository.reload()
        cached_path = self.root / "cache" / f"first-{WORKBOOK_NAME}"
        self.root_name = "_Журнали"
        self.resource.list.reset_mock()
        self.provider._download.reset_mock()

        repository.reload()
        repository.reload()

        self.resource.list.assert_not_called()
        self.provider._download.assert_not_called()
        self.assertEqual(repository.get_cached_student_grades("student@example.com").entries, ())
        deletions = [record for record in self.log_records() if record.get("action") == "delete"]
        self.assertEqual(len(deletions), 1)
        self.assertTrue(deletions[0]["cache_file_retained"])
        self.assertTrue(cached_path.exists())

    def test_renamed_nested_directory_is_removed_from_active_cache(self):
        directory = folder_info("course", "Курс")
        self.pages[("root", None)] = {"files": [directory]}
        self.pages[("course", None)] = {"files": [file_info("first")]}
        self.payloads["first"] = workbook_bytes()
        repository = GradesRepository(self.provider, update_log=self.log)
        repository.reload()
        directory["name"] = "_Курс"
        self.resource.list.reset_mock()
        self.provider._download.reset_mock()

        repository.reload()

        self.assertEqual(self.listed_folders(), ["root"])
        self.provider._download.assert_not_called()
        self.assertEqual(repository.get_cached_student_grades("student@example.com").disciplines, ())
        self.assertEqual(self.log_records()[-1]["action"], "delete")

    def test_nested_listing_error_preserves_previous_snapshot_and_inventory(self):
        self.pages[("root", None)] = {"files": [folder_info("course", "Курс")]}
        self.pages[("course", None)] = {"files": [file_info("first")]}
        self.payloads["first"] = workbook_bytes()
        repository = GradesRepository(self.provider, update_log=self.log)
        repository.reload()
        previous_time = repository.cache_info()[0]
        previous_inventory = self.log.inventory_path.read_bytes()
        previous_log = self.log.path.read_bytes()
        self.pages[("course", None)]["nextPageToken"] = "failed-page"
        self.pages[("course", "failed-page")] = RuntimeError("Drive unavailable")
        self.provider._download.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "Drive unavailable"):
            repository.reload()

        self.provider._download.assert_not_called()
        self.assertEqual(repository.cache_info()[0], previous_time)
        self.assertEqual(self.log.inventory_path.read_bytes(), previous_inventory)
        self.assertEqual(self.log.path.read_bytes(), previous_log)
        self.assertEqual(len(repository.get_cached_student_grades("student@example.com").entries), 1)

    def test_root_metadata_error_does_not_clear_cache(self):
        self.pages[("root", None)] = {"files": [file_info("first")]}
        self.payloads["first"] = workbook_bytes()
        repository = GradesRepository(self.provider, update_log=self.log)
        repository.reload()
        previous_time = repository.cache_info()[0]
        previous_inventory = self.log.inventory_path.read_bytes()
        self.resource.get.return_value.execute.side_effect = PermissionError("Drive folder inaccessible")
        self.resource.list.reset_mock()

        with self.assertRaises(PermissionError):
            repository.reload()

        self.resource.list.assert_not_called()
        self.assertEqual(repository.cache_info()[0], previous_time)
        self.assertEqual(self.log.inventory_path.read_bytes(), previous_inventory)
        self.assertEqual(len(repository.get_cached_student_grades("student@example.com").entries), 1)

    def test_each_directory_is_listed_only_once(self):
        self.pages[("root", None)] = {"files": [folder_info("course", "Курс"), folder_info("course", "Курс")]}
        self.pages[("course", None)] = {"files": [folder_info("root", "Журнали"), file_info("first")]}
        self.payloads["first"] = workbook_bytes()

        sources = self.provider.list_workbooks()

        self.assertEqual(len(sources), 1)
        self.assertEqual(self.listed_folders(), ["root", "course"])


if __name__ == "__main__":
    unittest.main()
