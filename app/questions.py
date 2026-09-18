from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

from .contracts import API_CONTRACT_VERSION, PublicQuestion, QuestionSummary


class QuestionError(ValueError):
    pass


class QuestionRepository:
    HIDDEN_FIELDS = {"reveal", "evaluation"}

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise QuestionError(f"문제 파일을 찾을 수 없습니다: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise QuestionError(f"문제 JSON이 올바르지 않습니다: {exc}") from exc
        questions = payload.get("questions") if isinstance(payload, dict) else None
        if not isinstance(questions, list) or not questions:
            raise QuestionError("questions 배열에 하나 이상의 문제가 필요합니다.")
        for question in questions:
            self._validate(question)
        question_ids = [question["id"] for question in questions]
        if len(question_ids) != len(set(question_ids)):
            raise QuestionError("문제 ID는 파일 안에서 고유해야 합니다.")
        return questions

    @staticmethod
    def _validate(question: dict[str, Any]) -> None:
        if not isinstance(question, dict):
            raise QuestionError("각 문제는 JSON 객체여야 합니다.")
        required = ("id", "version", "title", "scenario", "options", "reveal", "evaluation")
        missing = [key for key in required if key not in question]
        if missing:
            raise QuestionError(f"문제 필수 필드가 없습니다: {', '.join(missing)}")
        question_id = question["id"]
        if not isinstance(question_id, str) or not question_id.strip():
            raise QuestionError("문제 ID는 비어 있지 않은 문자열이어야 합니다.")
        version = question["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise QuestionError(f"{question_id}: 문제 버전은 1 이상의 정수여야 합니다.")
        for field in ("title", "scenario"):
            if not isinstance(question[field], str) or not question[field].strip():
                raise QuestionError(f"{question_id}: {field} 값이 필요합니다.")
        if not isinstance(question["options"], list) or len(question["options"]) < 2:
            raise QuestionError(f"{question_id}: 선택지가 2개 이상 필요합니다.")
        if any(not isinstance(option, dict) for option in question["options"]):
            raise QuestionError(f"{question_id}: 각 선택지는 JSON 객체여야 합니다.")
        option_ids = [option.get("id") for option in question["options"]]
        if (
            any(not isinstance(option_id, str) or not option_id.strip() for option_id in option_ids)
            or len(option_ids) != len(set(option_ids))
        ):
            raise QuestionError(f"{question_id}: 선택지 ID는 비어 있지 않고 고유해야 합니다.")
        if any(not isinstance(option.get("label"), str) or not option["label"].strip() for option in question["options"]):
            raise QuestionError(f"{question_id}: 모든 선택지에 label이 필요합니다.")

    def get(self, question_id: str) -> dict[str, Any]:
        for question in self._load():
            if question["id"] == question_id:
                return copy.deepcopy(question)
        raise QuestionError("문제를 찾을 수 없습니다.")

    def current(self) -> dict[str, Any]:
        questions = self._load()
        active = [question for question in questions if question.get("active", True)]
        if not active:
            raise QuestionError("활성 문제가 없습니다.")
        return copy.deepcopy(active[0])

    def active(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(question)
            for question in self._load()
            if question.get("active", True)
        ]

    def summaries(self) -> list[QuestionSummary]:
        summaries: list[QuestionSummary] = []
        for question in self.active():
            source = question.get("source", {})
            summaries.append(
                {
                    "id": question["id"],
                    "version": question["version"],
                    "title": question["title"],
                    "publisher": source.get("publisher", "출처 미상"),
                    "published_at": source.get("published_at", "확인되지 않음"),
                    "evidence_level": source.get("evidence_level", "확인되지 않음"),
                }
            )
        return summaries

    @classmethod
    def public_view(cls, question: dict[str, Any]) -> PublicQuestion:
        public = {
            key: copy.deepcopy(value) for key, value in question.items() if key not in cls.HIDDEN_FIELDS
        }
        public["contract_version"] = API_CONTRACT_VERSION
        return cast(PublicQuestion, public)
