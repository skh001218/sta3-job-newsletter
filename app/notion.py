from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .db import Database
from .service import AttemptService, ServiceError, canonical_json, utc_now


class NotionError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status is None or self.status == 429 or (self.status is not None and self.status >= 500)


class NotionClient:
    def __init__(self, token: str, api_version: str, timeout: int = 20):
        self.token = token
        self.api_version = api_version
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise NotionError("NOTION_TOKEN이 설정되지 않았습니다.", 401)
        body = canonical_json(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"https://api.notion.com/v1{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.api_version,
                "Content-Type": "application/json",
                "User-Agent": "game-da-practice/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("message", raw)
            except json.JSONDecodeError:
                detail = raw
            retry_header = exc.headers.get("Retry-After")
            retry_after = int(retry_header) if retry_header and retry_header.isdigit() else None
            raise NotionError(f"Notion API {exc.code}: {detail}", exc.code, retry_after) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NotionError(f"Notion API 연결 실패: {exc}") from exc

    @staticmethod
    def _rich_text(block: dict[str, Any]) -> str:
        data = block.get(block.get("type", ""), {})
        return "".join(item.get("plain_text", "") for item in data.get("rich_text", []))

    def find_attempt_block(self, page_id: str, attempt_id: str) -> str | None:
        marker = f"attempt:{attempt_id}"
        cursor: str | None = None
        while True:
            suffix = "?page_size=100"
            if cursor:
                suffix += f"&start_cursor={cursor}"
            result = self.request("GET", f"/blocks/{page_id}/children{suffix}")
            for block in result.get("results", []):
                if marker in self._rich_text(block):
                    return block.get("id")
            if not result.get("has_more"):
                return None
            cursor = result.get("next_cursor")

    def append_attempt(self, page_id: str, attempt: dict[str, Any]) -> str:
        children = [build_attempt_toggle(attempt)]
        result = self.request(
            "PATCH",
            f"/blocks/{page_id}/children",
            {"children": children, "position": {"type": "end"}},
        )
        created = result.get("results", [])
        if not created or not created[0].get("id"):
            raise NotionError("Notion이 추가된 블록 ID를 반환하지 않았습니다.")
        return created[0]["id"]

    def verify_block(self, block_id: str, attempt_id: str) -> None:
        block = self.request("GET", f"/blocks/{block_id}")
        if f"attempt:{attempt_id}" not in self._rich_text(block):
            raise NotionError("Notion 블록은 추가되었지만 시도 ID를 검증하지 못했습니다.")

    def mark_read(self, page_id: str, property_name: str) -> None:
        if not property_name:
            return
        self.request(
            "PATCH",
            f"/pages/{page_id}",
            {"properties": {property_name: {"checkbox": True}}},
        )


def _text_object(content: str, url: str | None = None) -> dict[str, Any]:
    text: dict[str, Any] = {"content": content}
    if url:
        text["link"] = {"url": url}
    return {"type": "text", "text": text}


def _chunks(value: str, size: int = 1800) -> list[str]:
    value = value or "-"
    return [value[index : index + size] for index in range(0, len(value), size)] or ["-"]


def _paragraphs(label: str, value: str) -> list[dict[str, Any]]:
    chunks = _chunks(value)
    blocks = []
    for index, chunk in enumerate(chunks):
        rich_text = []
        if index == 0:
            rich_text.append({**_text_object(f"{label}: "), "annotations": {"bold": True}})
        rich_text.append(_text_object(chunk))
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}})
    return blocks


def _list_text(values: list[str]) -> str:
    return "\n".join(f"• {value}" for value in values) if values else "-"


def _safe_url(value: str) -> str | None:
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def build_attempt_toggle(attempt: dict[str, Any]) -> dict[str, Any]:
    question = attempt["question"]
    response = attempt["response"]
    comparison = attempt["comparison"] or {}
    options = {item["id"]: item["label"] for item in question.get("options", [])}
    selected = response["selected_option"]
    timestamp = attempt.get("completed_at") or attempt.get("comparison_ready_at") or attempt["created_at"]
    title = f"풀이 기록 {timestamp[:10]} · attempt:{attempt['attempt_id']}"

    children: list[dict[str, Any]] = []
    children += _paragraphs("질문 버전", str(question["version"]))
    children += _paragraphs("질문", f"{question['title']}\n{question.get('prompt', '')}")
    children += _paragraphs("선택", f"{selected}. {options.get(selected, selected)}")
    children += _paragraphs("선택 이유", response["reason"])
    children += _paragraphs("예상 결과", response["expected_outcome"])
    children += _paragraphs("가정", response["assumptions"])
    children += _paragraphs("확신도", f"{response['confidence']}%")
    children += _paragraphs("실제 조치", comparison.get("actual_action", "-"))
    children += _paragraphs("실제 결과", comparison.get("actual_outcome", "-"))
    children += _paragraphs("지표", _list_text(comparison.get("metrics", [])))
    children += _paragraphs("잘 고려한 점", _list_text(comparison.get("well_considered", [])))
    children += _paragraphs("더 고려할 점", _list_text(comparison.get("missing_considerations", [])))
    children += _paragraphs(
        "결과와 별개로 합리적인 판단", _list_text(comparison.get("reasonable_despite_outcome", []))
    )
    children += _paragraphs("확인할 수 없는 점", _list_text(comparison.get("unknown_from_evidence", [])))
    children += _paragraphs("다음에 확인할 질문", _list_text(comparison.get("next_questions", [])))

    source = question.get("source", {})
    source_url = _safe_url(source.get("url", ""))
    source_text = f"{source.get('title', '-')} · {source.get('publisher', '-')}"
    source_rich_text = [_text_object(source_text, source_url)]
    children.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": source_rich_text}})

    return {
        "object": "block",
        "type": "toggle",
        "toggle": {"rich_text": [_text_object(title)], "children": children},
    }


class NotionSyncService:
    def __init__(self, db: Database, attempts: AttemptService, settings: Settings):
        self.db = db
        self.attempts = attempts
        self.settings = settings

    def enqueue(self, attempt_id: str, idempotency_key: str) -> dict[str, Any]:
        attempt = self.attempts.get(attempt_id)
        if attempt["status"] != "COMPARISON_READY":
            raise ServiceError("비교 결과를 확인한 뒤 저장할 수 있습니다.", 409)
        now = utc_now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM notion_syncs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing:
                if existing["idempotency_key"] != idempotency_key:
                    connection.commit()
                    raise ServiceError("이 풀이에 다른 저장 요청이 이미 있습니다.", 409)
            else:
                connection.execute(
                    """
                    INSERT INTO notion_syncs
                    (attempt_id, idempotency_key, status, next_retry_at, updated_at)
                    VALUES (?, ?, 'PENDING', ?, ?)
                    """,
                    (attempt_id, idempotency_key, now, now),
                )
                connection.execute(
                    "UPDATE attempts SET completed_at = ?, notion_status = 'PENDING' WHERE id = ?",
                    (now, attempt_id),
                )
            connection.commit()
        self.sync_once(attempt_id)
        return self.attempts.get(attempt_id)

    def retry(self, attempt_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT status FROM notion_syncs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if not row:
                raise ServiceError("저장 요청을 찾을 수 없습니다.", 404)
            if row["status"] == "SAVED":
                return self.attempts.get(attempt_id)
            connection.execute(
                """
                UPDATE notion_syncs
                SET status = 'PENDING', retry_count = 0, next_retry_at = ?, last_error = NULL, updated_at = ?
                WHERE attempt_id = ?
                """,
                (utc_now(), utc_now(), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET notion_status = 'PENDING', last_error = NULL WHERE id = ?",
                (attempt_id,),
            )
        self.sync_once(attempt_id)
        return self.attempts.get(attempt_id)

    def sync_once(self, attempt_id: str) -> bool:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sync = connection.execute(
                "SELECT * FROM notion_syncs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if not sync or sync["status"] not in {"PENDING", "RETRY"}:
                connection.commit()
                return bool(sync and sync["status"] == "SAVED")
            if sync["retry_count"] >= self.settings.max_retries:
                connection.execute(
                    "UPDATE notion_syncs SET status = 'FAILED', updated_at = ? WHERE attempt_id = ?",
                    (utc_now(), attempt_id),
                )
                connection.execute(
                    "UPDATE attempts SET notion_status = 'FAILED' WHERE id = ?", (attempt_id,)
                )
                connection.commit()
                return False
            retry_count = sync["retry_count"] + 1
            connection.execute(
                """
                UPDATE notion_syncs SET status = 'SYNCING', retry_count = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (retry_count, utc_now(), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET notion_status = 'SYNCING' WHERE id = ?", (attempt_id,)
            )
            connection.commit()

        attempt = self.attempts.get(attempt_id)
        page_id = attempt["question"].get("source", {}).get("notion_page_id") or self.settings.notion_page_id
        try:
            if not page_id:
                raise NotionError("NOTION_PAGE_ID 또는 문제의 source.notion_page_id가 필요합니다.", 400)
            client = NotionClient(self.settings.notion_token, self.settings.notion_api_version)
            block_id = client.find_attempt_block(page_id, attempt_id)
            if not block_id:
                block_id = client.append_attempt(page_id, attempt)
            client.verify_block(block_id, attempt_id)
            client.mark_read(page_id, self.settings.notion_read_property)
            notion_url = f"https://www.notion.so/{page_id.replace('-', '')}"
            now = utc_now()
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE notion_syncs
                    SET status = 'SAVED', next_retry_at = NULL, last_error = NULL, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (now, attempt_id),
                )
                connection.execute(
                    """
                    UPDATE attempts
                    SET notion_status = 'SAVED', notion_page_id = ?, notion_block_id = ?,
                        notion_url = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (page_id, block_id, notion_url, attempt_id),
                )
                connection.commit()
            return True
        except NotionError as exc:
            delay = exc.retry_after or min(3600, 2 ** min(retry_count, 10))
            next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            status = "RETRY" if exc.transient and retry_count < self.settings.max_retries else "FAILED"
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE notion_syncs
                    SET status = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (status, next_retry if status == "RETRY" else None, str(exc)[:1000], utc_now(), attempt_id),
                )
                connection.execute(
                    "UPDATE attempts SET notion_status = ?, last_error = ? WHERE id = ?",
                    (status, str(exc)[:1000], attempt_id),
                )
                connection.commit()
            return False

    def process_due(self) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT attempt_id FROM notion_syncs
                WHERE status IN ('PENDING', 'RETRY')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY updated_at ASC LIMIT 10
                """,
                (now,),
            ).fetchall()
        for row in rows:
            self.sync_once(row["attempt_id"])


class NotionSyncWorker(threading.Thread):
    def __init__(self, service: NotionSyncService, interval_seconds: int):
        super().__init__(name="notion-sync-worker", daemon=True)
        self.service = service
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.service.process_due()
            except Exception:
                # 워커 예외이 HTTP 서버를 종료시키지 않도록 한다.
                pass
            self.stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()
