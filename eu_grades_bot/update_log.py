from __future__ import annotations

from datetime import datetime, timezone, tzinfo
import json
import logging
from pathlib import Path
from threading import Lock
from typing import Iterable, Literal


logger = logging.getLogger(__name__)


class UpdateLog:
    """Append UTF-8 JSON records and remember the last successful source inventory."""

    def __init__(self, path: Path, log_timezone: tzinfo = timezone.utc):
        self.path = path
        self.timezone = log_timezone
        self.inventory_path = path.with_name(".update_inventory.json")
        self._lock = Lock()

    def _write(self, event: str, **details) -> None:
        with self._lock:
            record = {
                "timestamp": datetime.now(self.timezone).isoformat(timespec="milliseconds"),
                "event": event,
                **details,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_cache_update(
        self,
        drive_file: str,
        cache_file: Path,
        action: Literal["new", "update", "delete"],
        **details,
    ) -> None:
        if action not in {"new", "update", "delete"}:
            raise ValueError(f"Unknown cache update action: {action}")
        self._write(
            "cache_update",
            drive_file=drive_file,
            cache_file=cache_file.name,
            cache_path=str(cache_file.resolve()),
            action=action,
            **details,
        )

    def log_parse_result(
        self,
        source_file: str,
        cache_file: Path,
        *,
        drive_file: str | None,
        status: Literal["success", "error"],
        **summary,
    ) -> None:
        self._write(
            "parse_result",
            source_file=source_file,
            drive_file=drive_file,
            cache_file=cache_file.name,
            cache_path=str(cache_file.resolve()),
            status=status,
            **summary,
        )

    def reconcile_sources(self, sources: Iterable[tuple[str, Path]]) -> None:
        """Log logical removals only after the entire repository refresh succeeds.

        Cached files are retained; this records removal from the active inventory,
        not deletion from Google Drive or physical deletion from local storage.
        """
        current = {str(path.resolve()): name for name, path in sources}
        previous = self._load_inventory()
        for cached_path, drive_name in previous.items():
            if cached_path not in current:
                path = Path(cached_path)
                self.log_cache_update(
                    drive_name,
                    path,
                    "delete",
                    reason="removed_from_active_sources",
                    cache_file_retained=path.exists(),
                )
        self.inventory_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.inventory_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(current, handle, ensure_ascii=False, indent=2)
        temporary.replace(self.inventory_path)

    def _load_inventory(self) -> dict[str, str]:
        if not self.inventory_path.exists():
            return {}
        try:
            payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in payload.items()
            ):
                raise ValueError("Invalid update log inventory")
            return payload
        except (OSError, ValueError):
            logger.exception("Could not read update log inventory; establishing a new baseline.")
            return {}
