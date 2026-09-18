from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .contracts import (
    API_CONTRACT_VERSION,
    AttemptAnswer,
    AttemptStatus,
    AttemptView,
    ComparisonResult,
    PublicQuestion,
)
from .db import Database
from .questions import QuestionError, QuestionRepository


class ServiceError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AttemptService:
    RESPONSE_FIELDS = (
        "selected_option",
        "reason",
        "expected_outcome",
        "assumptions",
        "confidence",
    )

    def __init__(self, db: Database, questions: QuestionRepository):
        self.db = db
        self.questions = questions

    def get_public_question(self, question_id: str | None = None) -> PublicQuestion:
        try:
            question = self.questions.get(question_id) if question_id else self.questions.current()
        except QuestionError as exc:
            raise ServiceError(str(exc), 404) from exc
        return self.questions.public_view(question)

    def submit(self, payload: dict[str, Any]) -> AttemptView:
        idempotency_key = self._required_text(payload, "idempotency_key", 200)
        question_id = self._required_text(payload, "question_id", 200)
        try:
            version = int(payload.get("question_version"))
        except (TypeError, ValueError) as exc:
            raise ServiceError("문제 버전이 필요합니다.") from exc
        response = payload.get("response")
        if not isinstance(response, dict):
            raise ServiceError("답변 데이터가 필요합니다.")

        try:
            question = self.questions.get(question_id)
        except QuestionError as exc:
            raise ServiceError(str(exc), 404) from exc
        if version != int(question["version"]):
            raise ServiceError("문제가 갱신되었습니다. 새로고침 후 다시 제출해 주세요.", 409)

        normalized = self._normalize_response(response, question)
        request_body = {
            "question_id": question_id,
            "question_version": version,
            "response": normalized,
        }
        request_hash = hashlib.sha256(canonical_json(request_body).encode("utf-8")).hexdigest()
        attempt_id = str(uuid.uuid4())
        created_at = utc_now()

        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM attempts WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                connection.commit()
                if existing["request_hash"] != request_hash:
                    raise ServiceError("같은 제출 키가 다른 답변에 재사용되었습니다.", 409)
                return self._serialize(existing)
            connection.execute(
                """
                INSERT INTO attempts (
                    id, idempotency_key, request_hash, question_id, question_version,
                    question_snapshot, response_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    idempotency_key,
                    request_hash,
                    question_id,
                    version,
                    canonical_json(question),
                    canonical_json(normalized),
                    AttemptStatus.SUBMITTED.value,
                    created_at,
                ),
            )
            connection.commit()

        try:
            return self._generate_comparison(attempt_id)
        except ServiceError:
            # 제출 자체는 이미 확정되었다. 브라우저가 attempt_id를
            # 보존하고 비교 생성만 재시도할 수 있게 현재 상태를 반환한다.
            return self.get(attempt_id)

    def retry_comparison(self, attempt_id: str) -> AttemptView:
        attempt = self.get(attempt_id)
        if attempt["status"] == AttemptStatus.COMPARISON_READY:
            return attempt
        return self._generate_comparison(attempt_id)

    def _generate_comparison(self, attempt_id: str) -> AttemptView:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not row:
            raise ServiceError("풀이 기록을 찾을 수 없습니다.", 404)
        if row["status"] == AttemptStatus.COMPARISON_READY:
            return self._serialize(row)

        try:
            question = json.loads(row["question_snapshot"])
            response = json.loads(row["response_json"])
            evaluation = question["evaluation"]
            choice = evaluation.get("by_option", {}).get(response["selected_option"], {})
            comparison: ComparisonResult = {
                "actual_action": question["reveal"]["actual_action"],
                "actual_outcome": question["reveal"]["actual_outcome"],
                "metrics": question["reveal"].get("metrics", []),
                "recommended_option": question["reveal"].get("recommended_option", ""),
                "recommendation_reason": question["reveal"].get("recommendation_reason", ""),
                "perspective_before": question["reveal"].get("perspective_before", ""),
                "perspective_after": question["reveal"].get("perspective_after", ""),
                "well_considered": choice.get("well_considered", []),
                "missing_considerations": choice.get("missing_considerations", []),
                "reasonable_despite_outcome": choice.get("reasonable_despite_outcome", []),
                "unknown_from_evidence": evaluation.get("unknown_from_evidence", []),
                "next_questions": evaluation.get("next_questions", []),
                "evidence_links": question["reveal"].get("evidence_links", []),
                "rubric_version": evaluation.get("rubric_version", 1),
            }
            ready_at = utc_now()
            with self.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE attempts
                    SET comparison_json = ?, status = ?,
                        comparison_ready_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (
                        canonical_json(comparison),
                        AttemptStatus.COMPARISON_READY.value,
                        ready_at,
                        attempt_id,
                    ),
                )
                connection.commit()
        except Exception as exc:
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE attempts SET status = ?, last_error = ? WHERE id = ?",
                    (AttemptStatus.COMPARISON_FAILED.value, str(exc)[:1000], attempt_id),
                )
            raise ServiceError("답변은 저장되었지만 비교 결과를 만들지 못했습니다.", 500) from exc
        return self.get(attempt_id)

    def get(self, attempt_id: str) -> AttemptView:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            sync = connection.execute(
                "SELECT * FROM notion_syncs WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        if not row:
            raise ServiceError("풀이 기록을 찾을 수 없습니다.", 404)
        result = self._serialize(row)
        if sync:
            result["notion_sync"] = dict(sync)
        return result

    @staticmethod
    def _serialize(row: sqlite3.Row) -> AttemptView:
        snapshot = json.loads(row["question_snapshot"])
        result = {
            "contract_version": API_CONTRACT_VERSION,
            "attempt_id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "comparison_ready_at": row["comparison_ready_at"],
            "completed_at": row["completed_at"],
            "question": QuestionRepository.public_view(snapshot),
            "response": json.loads(row["response_json"]),
            "comparison": json.loads(row["comparison_json"]) if row["comparison_json"] else None,
            "notion_status": row["notion_status"],
            "notion_url": row["notion_url"],
            "last_error": row["last_error"],
        }
        return result

    @classmethod
    def _normalize_response(
        cls, response: dict[str, Any], question: dict[str, Any]
    ) -> AttemptAnswer:
        selected = cls._required_text(response, "selected_option", 200)
        valid_options = {option["id"] for option in question["options"]}
        if selected not in valid_options:
            raise ServiceError("올바른 선택지를 골라 주세요.")
        reason = cls._required_text(response, "reason", 10000)
        expected_outcome = cls._required_text(response, "expected_outcome", 10000)
        assumptions = cls._required_text(response, "assumptions", 10000)
        try:
            confidence = int(response.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ServiceError("확신도는 0~100 숫자여야 합니다.") from exc
        if confidence < 0 or confidence > 100:
            raise ServiceError("확신도는 0~100 범위여야 합니다.")
        return {
            "selected_option": selected,
            "reason": reason,
            "expected_outcome": expected_outcome,
            "assumptions": assumptions,
            "confidence": confidence,
        }

    @staticmethod
    def _required_text(payload: dict[str, Any], field: str, max_length: int) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ServiceError(f"{field} 값이 필요합니다.")
        value = value.strip()
        if len(value) > max_length:
            raise ServiceError(f"{field}은(는) {max_length}자 이하여야 합니다.")
        return value
