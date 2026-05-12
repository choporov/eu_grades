from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path


GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class WorkbookSource:
    title: str
    path: Path


class LocalWorkbookProvider:
    def __init__(self, folder: Path):
        self.folder = folder

    def list_workbooks(self) -> list[WorkbookSource]:
        if not self.folder.exists():
            raise FileNotFoundError(f"Grades folder does not exist: {self.folder}")
        files = sorted(
            [
                *self.folder.glob("*.xlsx"),
                *self.folder.glob("*.xlsm"),
            ],
            key=lambda p: p.name.lower(),
        )
        return [WorkbookSource(title=path.stem, path=path) for path in files if not path.name.startswith("~$")]


class DriveWorkbookProvider:
    def __init__(self, folder_id: str, credentials_file: Path, cache_dir: Path):
        self.folder_id = folder_id
        self.credentials_file = credentials_file
        self.cache_dir = cache_dir

    def list_workbooks(self) -> list[WorkbookSource]:
        service = self._build_service()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        files = self._list_drive_files(service)
        workbooks: list[WorkbookSource] = []

        for file_info in files:
            file_id = file_info["id"]
            name = file_info["name"]
            mime_type = file_info.get("mimeType")
            safe_name = self._safe_filename(name)
            if not safe_name.lower().endswith(".xlsx"):
                safe_name = f"{safe_name}.xlsx"
            target = self.cache_dir / f"{file_id}-{safe_name}"

            if mime_type == GOOGLE_SHEETS_MIME:
                request = service.files().export_media(fileId=file_id, mimeType=XLSX_MIME)
            else:
                request = service.files().get_media(fileId=file_id)

            self._download(request, target)
            workbooks.append(WorkbookSource(title=Path(name).stem, path=target))

        return workbooks

    def _build_service(self):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive mode requires google-api-python-client and google-auth."
            ) from exc

        scopes = ["https://www.googleapis.com/auth/drive.readonly"]
        credentials = service_account.Credentials.from_service_account_file(
            self.credentials_file,
            scopes=scopes,
        )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _list_drive_files(self, service) -> list[dict]:
        query = (
            f"'{self.folder_id}' in parents and trashed = false and "
            f"(mimeType = '{GOOGLE_SHEETS_MIME}' or mimeType = '{XLSX_MIME}')"
        )
        result: list[dict] = []
        page_token = None
        while True:
            response = (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                    pageToken=page_token,
                    pageSize=1000,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            result.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return result

    @staticmethod
    def _download(request, target: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        target.write_bytes(buffer.getvalue())

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", name).strip()
        return cleaned or "workbook"
