from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


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
        return questions

    @staticmethod
    def _validate(question: dict[str, Any]) -> None:
        required = ("id", "version", "title", "scenario", "options", "reveal", "evaluation")
        missing = [key for key in required if key not in question]
        if missing:
            raise QuestionError(f"문제 필수 필드가 없습니다: {', '.join(missing)}")
        if not isinstance(question["options"], list) or len(question["options"]) < 2:
            raise QuestionError(f"{question['id']}: 선택지가 2개 이상 필요합니다.")
        option_ids = [option.get("id") for option in question["options"]]
        if None in option_ids or len(option_ids) != len(set(option_ids)):
            raise QuestionError(f"{question['id']}: 선택지 ID는 고유해야 합니다.")

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

    @classmethod
    def public_view(cls, question: dict[str, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in question.items() if key not in cls.HIDDEN_FIELDS}
