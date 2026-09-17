from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """외부 패키지 없이 간단한 KEY=VALUE 형식을 읽는다."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _path_setting(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    access_token: str
    db_path: Path
    question_data_path: Path
    notion_token: str
    notion_page_id: str
    notion_read_property: str
    notion_api_version: str
    sync_interval_seconds: int
    max_retries: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        return cls(
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "8000")),
            access_token=os.getenv("APP_ACCESS_TOKEN", "").strip(),
            db_path=_path_setting("APP_DB_PATH", "data/app.db"),
            question_data_path=_path_setting("QUESTION_DATA_PATH", "data/questions.json"),
            notion_token=os.getenv("NOTION_TOKEN", "").strip(),
            notion_page_id=os.getenv("NOTION_PAGE_ID", "").strip(),
            notion_read_property=os.getenv("NOTION_READ_PROPERTY", "읽음").strip(),
            notion_api_version=os.getenv("NOTION_API_VERSION", "2026-03-11").strip(),
            sync_interval_seconds=max(2, int(os.getenv("NOTION_SYNC_INTERVAL_SECONDS", "10"))),
            max_retries=max(1, int(os.getenv("NOTION_MAX_RETRIES", "6"))),
        )
