from pathlib import Path
import unittest

from eu_grades_bot.drive import (
    GOOGLE_SHEETS_MIME,
    GOOGLE_SHORTCUT_MIME,
    DriveWorkbookProvider,
    LocalWorkbookProvider,
    extract_google_sheet_id_from_gsheet,
    is_gsheet_metadata_file,
    normalize_drive_id,
)


class DriveWorkbookProviderTest(unittest.TestCase):
    def test_normalize_drive_id_accepts_raw_id_and_folder_url(self):
        self.assertEqual(normalize_drive_id("abc123"), "abc123")
        self.assertEqual(
            normalize_drive_id("https://drive.google.com/drive/folders/abc123?usp=sharing"),
            "abc123",
        )
        self.assertEqual(
            normalize_drive_id("https://drive.google.com/open?id=abc123"),
            "abc123",
        )
        self.assertEqual(
            normalize_drive_id("https://docs.google.com/spreadsheets/d/sheet123/edit"),
            "sheet123",
        )

    def test_extracts_google_sheet_id_from_gsheet_json(self):
        with self.subTest("url"):
            path = self._write_gsheet(
                '{"url":"https://docs.google.com/spreadsheets/d/sheet-url-id/edit","doc_id":"sheet-doc-id"}'
            )
            self.assertEqual(extract_google_sheet_id_from_gsheet(path), "sheet-doc-id")

        with self.subTest("resource_id"):
            path = self._write_gsheet('{"resource_id":"spreadsheet:sheet-resource-id"}')
            self.assertEqual(extract_google_sheet_id_from_gsheet(path), "sheet-resource-id")

    def test_local_provider_requires_credentials_for_gsheet_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "Бази даних - Чопоров С.В..gsheet").write_text(
                '{"doc_id":"sheet-doc-id"}',
                encoding="utf-8",
            )
            provider = LocalWorkbookProvider(folder)

            with self.assertRaisesRegex(RuntimeError, r"\.gsheet files are shortcuts"):
                provider.list_workbooks()

    def test_resolves_google_sheet_shortcut(self):
        resolved = DriveWorkbookProvider._resolve_workbook_file(
            {
                "id": "shortcut-id",
                "name": "Бази даних - Чопоров С.В.",
                "mimeType": GOOGLE_SHORTCUT_MIME,
                "shortcutDetails": {
                    "targetId": "sheet-id",
                    "targetMimeType": GOOGLE_SHEETS_MIME,
                },
            }
        )

        self.assertEqual(
            resolved,
            {
                "id": "sheet-id",
                "name": "Бази даних - Чопоров С.В.",
                "mimeType": GOOGLE_SHEETS_MIME,
            },
        )

    def test_provider_normalizes_folder_id_on_init(self):
        provider = DriveWorkbookProvider(
            "https://drive.google.com/drive/folders/folder-id",
            Path("credentials.json"),
            Path("cache"),
        )

        self.assertEqual(provider.folder_id, "folder-id")

    def test_detects_drive_gsheet_metadata_files(self):
        self.assertTrue(
            is_gsheet_metadata_file(
                {
                    "name": "Бази даних - Чопоров С.В..gsheet",
                    "mimeType": "application/json",
                }
            )
        )
        self.assertFalse(
            is_gsheet_metadata_file(
                {
                    "name": "Бази даних - Чопоров С.В..gsheet",
                    "mimeType": GOOGLE_SHEETS_MIME,
                }
            )
        )

    @staticmethod
    def _write_gsheet(content: str) -> Path:
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".gsheet", delete=False, encoding="utf-8")
        with handle:
            handle.write(content)
        return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
