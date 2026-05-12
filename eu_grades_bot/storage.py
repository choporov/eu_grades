from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class AuthorizedUser:
    chat_id: int
    email: str
    created_at: str
    updated_at: str


class UserStorage:
    def __init__(self, path: Path):
        self.path = path

    def get(self, chat_id: int) -> AuthorizedUser | None:
        payload = self._load()
        raw = payload.get(str(chat_id))
        if not raw:
            return None
        return AuthorizedUser(
            chat_id=chat_id,
            email=raw["email"],
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
        )

    def set_email(self, chat_id: int, email: str) -> AuthorizedUser:
        payload = self._load()
        now = datetime.now(timezone.utc).isoformat()
        previous = payload.get(str(chat_id), {})
        payload[str(chat_id)] = {
            "email": email,
            "created_at": previous.get("created_at", now),
            "updated_at": now,
        }
        self._save(payload)
        return self.get(chat_id)  # type: ignore[return-value]

    def all_users(self) -> list[AuthorizedUser]:
        payload = self._load()
        users: list[AuthorizedUser] = []
        for raw_chat_id, raw in payload.items():
            try:
                chat_id = int(raw_chat_id)
            except ValueError:
                continue
            users.append(
                AuthorizedUser(
                    chat_id=chat_id,
                    email=raw["email"],
                    created_at=raw.get("created_at", ""),
                    updated_at=raw.get("updated_at", ""),
                )
            )
        return users

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary.replace(self.path)

