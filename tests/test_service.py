from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.notion import NotionSyncService, build_attempt_toggle
from app.questions import QuestionRepository
from app.service import AttemptService, ServiceError


ROOT = Path(__file__).resolve().parent.parent


class AttemptServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        self.db.initialize()
        self.service = AttemptService(self.db, QuestionRepository(ROOT / "data" / "questions.json"))
        self.question = self.service.get_public_question()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def payload(self, key: str = "submit-1", choice: str = "B") -> dict:
        return {
            "idempotency_key": key,
            "question_id": self.question["id"],
            "question_version": self.question["version"],
            "response": {
                "selected_option": choice,
                "reason": "원인을 먼저 확인하기 위해서",
                "expected_outcome": "이탈 구간을 알 수 있다",
                "assumptions": "코호트간 노출 차이가 크지 않다",
                "confidence": 70,
            },
        }

    def test_public_question_never_contains_reveal(self) -> None:
        self.assertNotIn("reveal", self.question)
        self.assertNotIn("evaluation", self.question)
        serialized = json.dumps(self.question, ensure_ascii=False)
        self.assertNotIn("실제 조치", serialized)

    def test_submit_is_idempotent(self) -> None:
        first = self.service.submit(self.payload())
        second = self.service.submit(self.payload())
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual("COMPARISON_READY", first["status"])
        self.assertTrue(first["comparison"])

    def test_same_key_with_different_answer_is_rejected(self) -> None:
        self.service.submit(self.payload())
        with self.assertRaises(ServiceError) as caught:
            self.service.submit(self.payload(choice="A"))
        self.assertEqual(409, caught.exception.status)

    def test_old_attempt_keeps_question_snapshot(self) -> None:
        attempt = self.service.submit(self.payload())
        original_title = attempt["question"]["title"]
        copied_questions = Path(self.temp_dir.name) / "questions.json"
        source = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))
        source["questions"][0]["title"] = "바뀐 제목"
        copied_questions.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        another_service = AttemptService(self.db, QuestionRepository(copied_questions))
        restored = another_service.get(attempt["attempt_id"])
        self.assertEqual(original_title, restored["question"]["title"])

    def test_invalid_confidence_is_rejected_before_insert(self) -> None:
        payload = self.payload()
        payload["response"]["confidence"] = 101
        with self.assertRaises(ServiceError):
            self.service.submit(payload)

    def test_notion_failure_does_not_remove_attempt(self) -> None:
        attempt = self.service.submit(self.payload())
        settings = Settings(
            host="127.0.0.1",
            port=8000,
            access_token="",
            db_path=self.db.path,
            question_data_path=ROOT / "data" / "questions.json",
            notion_token="",
            notion_page_id="",
            notion_read_property="읽음",
            notion_api_version="2026-03-11",
            sync_interval_seconds=10,
            max_retries=2,
        )
        sync = NotionSyncService(self.db, self.service, settings)
        result = sync.enqueue(attempt["attempt_id"], "save-1")
        self.assertEqual("FAILED", result["notion_status"])
        self.assertEqual(attempt["response"], self.service.get(attempt["attempt_id"])["response"])

    def test_notion_toggle_has_stable_attempt_marker(self) -> None:
        attempt = self.service.submit(self.payload())
        toggle = build_attempt_toggle(attempt)
        title = toggle["toggle"]["rich_text"][0]["text"]["content"]
        self.assertIn(f"attempt:{attempt['attempt_id']}", title)
        self.assertLessEqual(len(toggle["toggle"]["children"]), 100)


if __name__ == "__main__":
    unittest.main()
