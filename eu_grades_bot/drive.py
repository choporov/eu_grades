from __future__ import annotations

import io
import re
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Iterable

from .update_log import UpdateLog


GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUPPORTED_DRIVE_WORKBOOK_MIMES = {GOOGLE_SHEETS_MIME, XLSX_MIME}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkbookSource:
    title: str
    path: Path
    drive_name: str | None = None


class LocalWorkbookProvider:
    def __init__(
        self,
        folder: Path,
        credentials_file: Path | None = None,
        cache_dir: Path | None = None,
        update_log: UpdateLog | None = None,
    ):
        self.folder = folder
        self.credentials_file = credentials_file
        self.cache_dir = cache_dir
        self.update_log = update_log

    def list_workbooks(self) -> list[WorkbookSource]:
        if not self.folder.exists():
            raise FileNotFoundError(f"Grades folder does not exist: {self.folder}")
        if self.folder.name.startswith("_") or self.folder.resolve().name.startswith("_"):
            return []

        files = list(self._iter_files())
        workbook_files = sorted(
            (path for path in files if path.suffix.lower() in {".xlsx", ".xlsm"}),
            key=lambda p: (p.name.lower(), str(p)),
        )
        workbooks = [
            WorkbookSource(title=path.stem, path=path)
            for path in workbook_files
            if not path.name.startswith("~$")
        ]

        gsheet_files = sorted(
            (path for path in files if path.suffix.lower() == ".gsheet"),
            key=lambda p: (p.name.lower(), str(p)),
        )
        if gsheet_files:
            workbooks.extend(self._export_gsheet_files(gsheet_files))

        return workbooks

    def _iter_files(self) -> Iterable[Path]:
        # Exports are derived copies, not new sources on the next refresh.
        cache_dirs = {(self.folder / ".gsheet_cache").resolve()}
        if self.cache_dir is not None:
            cache_dirs.add(self.cache_dir.resolve())
        pending = [self.folder]
        while pending:
            folder = pending.pop()
            # Let listing errors abort the refresh instead of publishing a partial cache.
            for path in folder.iterdir():
                if path.is_dir():
                    if (
                        not path.name.startswith("_")
                        and not path.is_symlink()
                        and path.resolve() not in cache_dirs
                    ):
                        pending.append(path)
                elif path.is_file():
                    yield path

    def _export_gsheet_files(self, paths: list[Path]) -> list[WorkbookSource]:
        if not self.credentials_file:
            raise RuntimeError(
                "Local Google Drive .gsheet files are shortcuts, not spreadsheets. "
                "Set GOOGLE_APPLICATION_CREDENTIALS so the bot can export them, "
                "or use GRADES_SOURCE=drive."
            )
        cache_dir = self.cache_dir or (self.folder / ".gsheet_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        service = build_drive_service(self.credentials_file)

        workbooks: list[WorkbookSource] = []
        for path in paths:
            spreadsheet_id = extract_google_sheet_id_from_gsheet(path)
            if not spreadsheet_id:
                raise ValueError(f"Could not read Google Sheets id from {path}")

            safe_name = DriveWorkbookProvider._safe_filename(path.stem)
            target = cache_dir / f"{spreadsheet_id}-{safe_name}.xlsx"
            request = service.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
            action = "update" if target.exists() else "new"
            download_media(request, target)
            if self.update_log is not None:
                self.update_log.log_cache_update(path.name, target, action)
            workbooks.append(WorkbookSource(title=path.stem, path=target, drive_name=path.name))

        return workbooks


class DriveWorkbookProvider:
    def __init__(
        self,
        folder_id: str,
        credentials_file: Path,
        cache_dir: Path,
        update_log: UpdateLog | None = None,
    ):
        self.folder_id = normalize_drive_id(folder_id)
        self.credentials_file = credentials_file
        self.cache_dir = cache_dir
        self.update_log = update_log

    def list_workbooks(self) -> list[WorkbookSource]:
        service = self._build_service()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        files = self._list_drive_files(service)
        logger.info("Drive folder %s returned %d file(s).", self.folder_id, len(files))
        workbooks: list[WorkbookSource] = []

        for file_info in files:
            if is_gsheet_metadata_file(file_info):
                workbooks.append(self._export_drive_gsheet_metadata_file(service, file_info))
                continue

            workbook_file = self._resolve_workbook_file(file_info)
            if workbook_file is None:
                continue

            file_id = workbook_file["id"]
            name = workbook_file["name"]
            mime_type = workbook_file.get("mimeType")
            safe_name = self._safe_filename(name)
            if not safe_name.lower().endswith(".xlsx"):
                safe_name = f"{safe_name}.xlsx"
            target = self.cache_dir / f"{file_id}-{safe_name}"

            if mime_type == GOOGLE_SHEETS_MIME:
                request = service.files().export_media(fileId=file_id, mimeType=XLSX_MIME)
            else:
                request = service.files().get_media(fileId=file_id)

            self._download_workbook(request, target, name)
            workbooks.append(WorkbookSource(title=Path(name).stem, path=target, drive_name=name))

        logger.info("Drive folder %s produced %d workbook(s).", self.folder_id, len(workbooks))
        return workbooks

    def _export_drive_gsheet_metadata_file(self, service, file_info: dict) -> WorkbookSource:
        file_id = file_info["id"]
        name = file_info.get("name", "workbook.gsheet")
        safe_name = self._safe_filename(name)
        metadata_target = self.cache_dir / f"{file_id}-{safe_name}"
        metadata_request = service.files().get_media(fileId=file_id)
        self._download(metadata_request, metadata_target)

        spreadsheet_id = extract_google_sheet_id_from_gsheet(metadata_target)
        if not spreadsheet_id:
            raise ValueError(f"Could not read Google Sheets id from Drive .gsheet file: {name}")

        xlsx_target = self.cache_dir / f"{spreadsheet_id}-{self._safe_filename(Path(name).stem)}.xlsx"
        export_request = service.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
        self._download_workbook(export_request, xlsx_target, name)
        return WorkbookSource(title=Path(name).stem, path=xlsx_target, drive_name=name)

    def _download_workbook(self, request, target: Path, drive_name: str) -> None:
        action = "update" if target.exists() else "new"
        self._download(request, target)
        if self.update_log is not None:
            self.update_log.log_cache_update(drive_name, target, action)

    def _build_service(self):
        return build_drive_service(self.credentials_file)

    def _list_drive_files(self, service) -> list[dict]:
        root = service.files().get(
            fileId=self.folder_id, fields="name", supportsAllDrives=True,
        ).execute()
        if root["name"].startswith("_"):
            return []

        result: list[dict] = []
        pending = [self.folder_id]
        visited: set[str] = set()
        while pending:
            folder_id = pending.pop()
            if folder_id in visited:
                continue
            visited.add(folder_id)
            query = f"'{folder_id}' in parents and trashed = false"
            page_token = None
            while True:
                response = (
                    service.files()
                    .list(
                        q=query,
                        corpora="allDrives",
                        spaces="drive",
                        fields=(
                            "nextPageToken, "
                            "files(id, name, mimeType, modifiedTime, "
                            "shortcutDetails(targetId,targetMimeType))"
                        ),
                        pageToken=page_token,
                        pageSize=1000,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                for file_info in response.get("files", []):
                    if file_info.get("mimeType") == GOOGLE_FOLDER_MIME:
                        if not file_info["name"].startswith("_"):
                            pending.append(file_info["id"])
                    else:
                        result.append(file_info)
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        return result

    @staticmethod
    def _resolve_workbook_file(file_info: dict) -> dict | None:
        mime_type = file_info.get("mimeType")
        if mime_type in SUPPORTED_DRIVE_WORKBOOK_MIMES:
            return file_info

        if mime_type != GOOGLE_SHORTCUT_MIME:
            return None

        shortcut_details = file_info.get("shortcutDetails") or {}
        target_mime_type = shortcut_details.get("targetMimeType")
        target_id = shortcut_details.get("targetId")
        if target_mime_type not in SUPPORTED_DRIVE_WORKBOOK_MIMES or not target_id:
            return None

        return {
            "id": target_id,
            "name": file_info.get("name", "workbook"),
            "mimeType": target_mime_type,
        }

    @staticmethod
    def _download(request, target: Path) -> None:
        download_media(request, target)

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", name).strip()
        return cleaned or "workbook"


def normalize_drive_id(value: str) -> str:
    value = value.strip()
    folder_match = re.search(r"/folders/([^/?#]+)", value)
    if folder_match:
        return folder_match.group(1)
    id_match = re.search(r"[?&]id=([^&#]+)", value)
    if id_match:
        return id_match.group(1)
    spreadsheet_match = re.search(r"/spreadsheets/d/([^/?#]+)", value)
    if spreadsheet_match:
        return spreadsheet_match.group(1)
    return value.rstrip("/")


def is_gsheet_metadata_file(file_info: dict) -> bool:
    name = str(file_info.get("name", "")).casefold()
    mime_type = file_info.get("mimeType")
    return name.endswith(".gsheet") and mime_type not in {
        GOOGLE_SHEETS_MIME,
        GOOGLE_SHORTCUT_MIME,
    }


def extract_google_sheet_id_from_gsheet(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {}

    if isinstance(payload, dict):
        for key in ("doc_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_drive_id(value)

        resource_id = payload.get("resource_id")
        if isinstance(resource_id, str) and ":" in resource_id:
            candidate = resource_id.rsplit(":", 1)[-1].strip()
            if candidate:
                return candidate

        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            return normalize_drive_id(url)

    normalized_text = text.replace("\\/", "/")
    spreadsheet_match = re.search(r"/spreadsheets/d/([^/?#\"']+)", normalized_text)
    if spreadsheet_match:
        return spreadsheet_match.group(1)

    doc_id_match = re.search(r'"doc_id"\s*:\s*"([^"]+)"', normalized_text)
    if doc_id_match:
        return normalize_drive_id(doc_id_match.group(1))

    resource_match = re.search(r'"resource_id"\s*:\s*"[^":]+:([^"]+)"', normalized_text)
    if resource_match:
        return resource_match.group(1)

    return None


def build_drive_service(credentials_file: Path):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive export requires google-api-python-client and google-auth."
        ) from exc

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=scopes,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def download_media(request, target: Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    target.write_bytes(buffer.getvalue())
