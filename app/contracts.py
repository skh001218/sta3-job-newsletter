from __future__ import annotations

from enum import StrEnum
from typing import Any, NotRequired, Protocol, TypedDict


# 질문 내용의 version과 별개인 HTTP API 응답 계약 버전이다.
# 필드를 제거하거나 의미를 바꾸는 호환성 파괴 변경에서만 증가시킨다.
API_CONTRACT_VERSION = 1


class AttemptStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    COMPARISON_READY = "COMPARISON_READY"
    COMPARISON_FAILED = "COMPARISON_FAILED"


class PersistenceStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    RETRY = "RETRY"
    SAVED = "SAVED"
    FAILED = "FAILED"


class QuestionOption(TypedDict):
    id: str
    label: str


class QuestionSource(TypedDict, total=False):
    title: str
    publisher: str
    url: str
    notion_page_id: str


class PublicQuestion(TypedDict):
    contract_version: int
    id: str
    version: int
    active: NotRequired[bool]
    title: str
    scenario: str
    prompt: NotRequired[str]
    options: list[QuestionOption]
    answer_fields: NotRequired[list[str]]
    source: NotRequired[QuestionSource]


class AttemptAnswer(TypedDict):
    selected_option: str
    reason: str
    expected_outcome: str
    assumptions: str
    confidence: int


class EvidenceLink(TypedDict):
    label: str
    url: str


class ComparisonResult(TypedDict):
    actual_action: str
    actual_outcome: str
    metrics: list[str]
    well_considered: list[str]
    missing_considerations: list[str]
    reasonable_despite_outcome: list[str]
    unknown_from_evidence: list[str]
    next_questions: list[str]
    evidence_links: list[EvidenceLink]
    rubric_version: int


class AttemptView(TypedDict):
    contract_version: int
    attempt_id: str
    status: str
    created_at: str
    comparison_ready_at: str | None
    completed_at: str | None
    question: PublicQuestion
    response: AttemptAnswer
    comparison: ComparisonResult | None
    notion_status: str
    notion_url: str | None
    last_error: str | None
    notion_sync: NotRequired[dict[str, Any]]


class AttemptApplication(Protocol):
    """웹 계층이 사용하는 문제 풀이 애플리케이션 경계."""

    def get_public_question(self, question_id: str | None = None) -> PublicQuestion: ...

    def submit(self, payload: dict[str, Any]) -> AttemptView: ...

    def retry_comparison(self, attempt_id: str) -> AttemptView: ...

    def get(self, attempt_id: str) -> AttemptView: ...


class AttemptArchive(Protocol):
    """완료된 풀이를 외부 저장소에 보관하는 경계."""

    def enqueue(self, attempt_id: str, idempotency_key: str) -> AttemptView: ...

    def retry(self, attempt_id: str) -> AttemptView: ...
