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
from .contracts import AttemptStatus, PersistenceStatus
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

    def _block_children(self, block_id: str) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            suffix = "?page_size=100"
            if cursor:
                suffix += f"&start_cursor={cursor}"
            result = self.request("GET", f"/blocks/{block_id}/children{suffix}")
            children.extend(result.get("results", []))
            if not result.get("has_more"):
                return children
            cursor = result.get("next_cursor")

    def resolve_data_source(self, container_id: str) -> dict[str, Any]:
        database: dict[str, Any] | None = None
        try:
            candidate = self.request("GET", f"/databases/{container_id}")
            if candidate.get("object") == "database":
                database = candidate
        except NotionError as exc:
            if exc.status not in {400, 404}:
                raise

        if database is None:
            child_databases = [
                block
                for block in self._block_children(container_id)
                if block.get("type") == "child_database"
            ]
            if len(child_databases) != 1:
                raise NotionError(
                    "저장 대상 페이지에는 인라인 데이터베이스가 정확히 하나 있어야 합니다.",
                    400,
                )
            database = self.request("GET", f"/databases/{child_databases[0]['id']}")

        data_sources = database.get("data_sources", [])
        if len(data_sources) != 1 or not data_sources[0].get("id"):
            raise NotionError("저장 대상 데이터베이스의 데이터 소스를 하나로 확인할 수 없습니다.", 400)
        data_source = self.request("GET", f"/data_sources/{data_sources[0]['id']}")
        return {
            "database_id": database["id"],
            "data_source_id": data_source["id"],
            "properties": data_source.get("properties", {}),
        }

    @staticmethod
    def _title_property(properties: dict[str, Any]) -> str:
        titles = [name for name, value in properties.items() if value.get("type") == "title"]
        if len(titles) != 1:
            raise NotionError("저장 대상 데이터베이스의 제목 속성을 하나로 확인할 수 없습니다.", 400)
        return titles[0]

    def create_attempt_page(
        self,
        data_source_id: str,
        properties: dict[str, Any],
        attempt: dict[str, Any],
        read_property: str,
    ) -> dict[str, Any]:
        toggle = build_attempt_toggle(attempt)
        result = self.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": build_attempt_database_properties(
                    attempt, properties, read_property
                ),
                "children": toggle["toggle"]["children"],
            },
        )
        if not result.get("id"):
            raise NotionError("Notion이 생성된 데이터베이스 행 ID를 반환하지 않았습니다.")
        return result

    def verify_attempt_page(
        self,
        page_id: str,
        data_source_id: str,
        properties: dict[str, Any],
        expected_title: str,
    ) -> dict[str, Any]:
        page = self.request("GET", f"/pages/{page_id}")
        title_property = self._title_property(properties)
        title = "".join(
            item.get("plain_text", "")
            for item in page.get("properties", {}).get(title_property, {}).get("title", [])
        )
        parent = page.get("parent", {})
        if title != expected_title or parent.get("data_source_id") != data_source_id:
            raise NotionError("생성된 Notion 데이터베이스 행을 검증하지 못했습니다.")
        return page


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


def _heading(content: str, level: int) -> dict[str, Any]:
    block_type = f"heading_{level}"
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [_text_object(content)]},
    }


def _bullets(values: list[str]) -> list[dict[str, Any]]:
    items = values or ["-"]
    return [
        {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [_text_object(value)]},
        }
        for value in items
    ]


def _bold_transition(before: str, after: str) -> dict[str, Any]:
    rich_text = [
        {**_text_object(before or "확인되지 않음"), "annotations": {"bold": True}},
        _text_object("에서 "),
        {**_text_object(after or "확인되지 않음"), "annotations": {"bold": True}},
        _text_object("으로 전환합니다."),
    ]
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _safe_url(value: str) -> str | None:
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def build_attempt_database_properties(
    attempt: dict[str, Any], schema: dict[str, Any], read_property: str
) -> dict[str, Any]:
    question = attempt["question"]
    source = question.get("source", {})
    title_properties = [name for name, value in schema.items() if value.get("type") == "title"]
    if len(title_properties) != 1:
        raise NotionError("저장 대상 데이터베이스의 제목 속성을 하나로 확인할 수 없습니다.", 400)

    title = question.get("title", "풀이 기록")
    properties: dict[str, Any] = {
        title_properties[0]: {"title": [_text_object(title)]},
    }

    if schema.get("발행처", {}).get("type") == "rich_text" and source.get("publisher"):
        properties["발행처"] = {"rich_text": [_text_object(source["publisher"])]}
    source_url = _safe_url(source.get("url", ""))
    if schema.get("원문", {}).get("type") == "url" and source_url:
        properties["원문"] = {"url": source_url}
    published_at = source.get("published_at", "")
    try:
        datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        pass
    else:
        if schema.get("발행일", {}).get("type") == "date":
            properties["발행일"] = {"date": {"start": published_at}}
    evidence_level = source.get("evidence_level", "")
    if schema.get("근거 수준", {}).get("type") == "select" and evidence_level:
        properties["근거 수준"] = {"select": {"name": evidence_level}}
    topics = source.get("related_topics", [])
    if schema.get("관련 주제", {}).get("type") == "multi_select" and topics:
        properties["관련 주제"] = {
            "multi_select": [{"name": topic} for topic in topics if isinstance(topic, str) and topic]
        }
    if read_property:
        if schema.get(read_property, {}).get("type") != "checkbox":
            raise NotionError(
                f"Notion 데이터베이스에 checkbox 속성 '{read_property}'이(가) 없습니다.",
                400,
            )
        properties[read_property] = {"checkbox": True}
    return properties


def build_attempt_toggle(attempt: dict[str, Any]) -> dict[str, Any]:
    question = attempt["question"]
    response = attempt["response"]
    comparison = attempt["comparison"] or {}
    options = {item["id"]: item["label"] for item in question.get("options", [])}
    selected = response["selected_option"]
    timestamp = attempt.get("completed_at") or attempt.get("comparison_ready_at") or attempt["created_at"]
    title = f"풀이 기록 {timestamp[:10]} · attempt:{attempt['attempt_id']}"

    source = question.get("source", {})
    source_url = _safe_url(source.get("url", ""))
    evidence_links = comparison.get("evidence_links", [])
    if not source_url and evidence_links:
        source_url = _safe_url(evidence_links[0].get("url", ""))

    recommended = comparison.get("recommended_option", "")
    recommended_label = options.get(recommended, recommended) if recommended else "확인되지 않음"
    recommended_text = (
        f"{recommended}. {recommended_label}" if recommended else recommended_label
    )
    recommendation_reason = comparison.get("recommendation_reason") or "추천 판단의 근거를 확인할 수 없습니다."

    reveal_children: list[dict[str, Any]] = [_heading("실제 사례", 3)]
    reveal_children += _paragraphs("실제 조치", comparison.get("actual_action", "-"))
    reveal_children += _paragraphs("실제 결과", comparison.get("actual_outcome", "-"))
    reveal_children += _paragraphs("주요 지표", _list_text(comparison.get("metrics", [])))
    reveal_children.append(_heading("추천 판단", 3))
    reveal_children += _paragraphs("선택지", recommended_text)
    reveal_children += _paragraphs("이유", recommendation_reason)
    reveal_children.append(_heading("관점 전환", 3))
    reveal_children.append(
        _bold_transition(
            comparison.get("perspective_before", ""),
            comparison.get("perspective_after", ""),
        )
    )
    reveal_children.append(_heading("내 답과 비교하기", 3))
    reveal_children += _bullets(
        [
            "처음 판단에서 놓쳤던 정보는 무엇이었나요?",
            "실제 결과를 본 뒤 답을 바꾸고 싶다면 무엇으로 바꾸고 싶나요?",
        ]
    )
    reveal_children.append(_heading("다음에 가져갈 질문", 3))
    reveal_children += _bullets(comparison.get("next_questions", []))
    reveal_children.append(_heading("참조", 3))
    reveal_children += _bullets(
        [
            f"자료명: {source.get('title', '-')}",
            f"발행처: {source.get('publisher', '-')}",
            f"발행일: {source.get('published_at', '확인되지 않음')}",
            f"근거 수준: {source.get('evidence_level', '확인되지 않음')}",
        ]
    )
    link_rich_text = [_text_object("원문 보기", source_url)] if source_url else [_text_object("원문 보기: 확인되지 않음")]
    reveal_children.append(
        {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": link_rich_text},
        }
    )

    scenario = question.get("scenario", "-")
    prompt = question.get("prompt", "")
    situation = f"{scenario}\n\n{prompt}" if prompt else scenario
    children: list[dict[str, Any]] = [_heading("상황", 2)]
    children += _paragraphs(question.get("title", "질문"), situation)
    children.append(_heading("내 답변", 2))
    children.append(_heading("선택한 이유", 3))
    children += _paragraphs("선택한 답", f"{selected}. {options.get(selected, selected)}")
    children += _paragraphs("이유", response["reason"])
    children.append(_heading("실제 답과 내 답변의 차이와 고려해볼 점", 3))
    children += _paragraphs("예상 결과", response["expected_outcome"])
    children += _paragraphs("가정", response["assumptions"])
    children += _paragraphs("확신도", f"{response['confidence']}%")
    children += _paragraphs("잘 고려한 점", _list_text(comparison.get("well_considered", [])))
    children += _paragraphs("놓친 점", _list_text(comparison.get("missing_considerations", [])))
    children += _paragraphs(
        "결과와 별개로 합리적인 판단", _list_text(comparison.get("reasonable_despite_outcome", []))
    )
    children += _paragraphs("확인할 수 없는 점", _list_text(comparison.get("unknown_from_evidence", [])))
    children.append(
        {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [_text_object("실제 결과와 관점 전환 보기")],
                "color": "green_background",
                "children": reveal_children,
            },
        }
    )

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
        if attempt["status"] != AttemptStatus.COMPARISON_READY:
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
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (attempt_id, idempotency_key, PersistenceStatus.PENDING.value, now, now),
                )
                connection.execute(
                    "UPDATE attempts SET completed_at = ?, notion_status = ? WHERE id = ?",
                    (now, PersistenceStatus.PENDING.value, attempt_id),
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
            if row["status"] == PersistenceStatus.SAVED:
                return self.attempts.get(attempt_id)
            connection.execute(
                """
                UPDATE notion_syncs
                SET status = ?, retry_count = 0, next_retry_at = ?, last_error = NULL, updated_at = ?
                WHERE attempt_id = ?
                """,
                (PersistenceStatus.PENDING.value, utc_now(), utc_now(), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET notion_status = ?, last_error = NULL WHERE id = ?",
                (PersistenceStatus.PENDING.value, attempt_id),
            )
        self.sync_once(attempt_id)
        return self.attempts.get(attempt_id)

    def sync_once(self, attempt_id: str) -> bool:
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sync = connection.execute(
                "SELECT * FROM notion_syncs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            pending_statuses = {PersistenceStatus.PENDING, PersistenceStatus.RETRY}
            if not sync or sync["status"] not in pending_statuses:
                connection.commit()
                return bool(sync and sync["status"] == PersistenceStatus.SAVED)
            if sync["retry_count"] >= self.settings.max_retries:
                connection.execute(
                    "UPDATE notion_syncs SET status = ?, updated_at = ? WHERE attempt_id = ?",
                    (PersistenceStatus.FAILED.value, utc_now(), attempt_id),
                )
                connection.execute(
                    "UPDATE attempts SET notion_status = ? WHERE id = ?",
                    (PersistenceStatus.FAILED.value, attempt_id),
                )
                connection.commit()
                return False
            retry_count = sync["retry_count"] + 1
            connection.execute(
                """
                UPDATE notion_syncs SET status = ?, retry_count = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (PersistenceStatus.SYNCING.value, retry_count, utc_now(), attempt_id),
            )
            connection.execute(
                "UPDATE attempts SET notion_status = ? WHERE id = ?",
                (PersistenceStatus.SYNCING.value, attempt_id),
            )
            stored_attempt = connection.execute(
                "SELECT notion_page_id, notion_url FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            connection.commit()

        attempt = self.attempts.get(attempt_id)
        page_id = attempt["question"].get("source", {}).get("notion_page_id") or self.settings.notion_page_id
        try:
            if not page_id:
                raise NotionError("NOTION_PAGE_ID 또는 문제의 source.notion_page_id가 필요합니다.", 400)
            client = NotionClient(self.settings.notion_token, self.settings.notion_api_version)
            target = client.resolve_data_source(page_id)
            notion_page: dict[str, Any] | None = None
            if stored_attempt and stored_attempt["notion_page_id"]:
                try:
                    notion_page = client.request(
                        "GET", f"/pages/{stored_attempt['notion_page_id']}"
                    )
                except NotionError as exc:
                    if exc.status != 404:
                        raise
            if notion_page is None:
                notion_page = client.create_attempt_page(
                    target["data_source_id"],
                    target["properties"],
                    attempt,
                    self.settings.notion_read_property,
                )
                created_page_id = notion_page["id"]
                created_url = notion_page.get("url") or (
                    f"https://www.notion.so/{created_page_id.replace('-', '')}"
                )
                with self.db.connect() as connection:
                    connection.execute(
                        """
                        UPDATE attempts
                        SET notion_page_id = ?, notion_block_id = ?, notion_url = ?
                        WHERE id = ?
                        """,
                        (created_page_id, created_page_id, created_url, attempt_id),
                    )
            notion_page = client.verify_attempt_page(
                notion_page["id"],
                target["data_source_id"],
                target["properties"],
                attempt["question"].get("title", "풀이 기록"),
            )
            notion_page_id = notion_page["id"]
            notion_url = notion_page.get("url") or f"https://www.notion.so/{notion_page_id.replace('-', '')}"
            now = utc_now()
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE notion_syncs
                    SET status = ?, next_retry_at = NULL, last_error = NULL, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (PersistenceStatus.SAVED.value, now, attempt_id),
                )
                connection.execute(
                    """
                    UPDATE attempts
                    SET notion_status = ?, notion_page_id = ?, notion_block_id = ?,
                        notion_url = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (
                        PersistenceStatus.SAVED.value,
                        notion_page_id,
                        notion_page_id,
                        notion_url,
                        attempt_id,
                    ),
                )
                connection.commit()
            return True
        except NotionError as exc:
            delay = exc.retry_after or min(3600, 2 ** min(retry_count, 10))
            next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            status = (
                PersistenceStatus.RETRY
                if exc.transient and retry_count < self.settings.max_retries
                else PersistenceStatus.FAILED
            )
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE notion_syncs
                    SET status = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        status.value,
                        next_retry if status == PersistenceStatus.RETRY else None,
                        str(exc)[:1000],
                        utc_now(),
                        attempt_id,
                    ),
                )
                connection.execute(
                    "UPDATE attempts SET notion_status = ?, last_error = ? WHERE id = ?",
                    (status.value, str(exc)[:1000], attempt_id),
                )
                connection.commit()
            return False

    def process_due(self) -> None:
        now = utc_now()
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT attempt_id FROM notion_syncs
                WHERE status IN (?, ?)
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY updated_at ASC LIMIT 10
                """,
                (PersistenceStatus.PENDING.value, PersistenceStatus.RETRY.value, now),
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
