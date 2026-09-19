from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.contracts import API_CONTRACT_VERSION
from app.db import Database
from app.notion import (
    NotionClient,
    NotionError,
    NotionSyncService,
    build_attempt_database_properties,
    build_attempt_toggle,
)
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
        self.assertEqual(API_CONTRACT_VERSION, self.question["contract_version"])
        self.assertNotIn("reveal", self.question)
        self.assertNotIn("evaluation", self.question)
        serialized = json.dumps(self.question, ensure_ascii=False)
        self.assertNotIn("실제 조치", serialized)

    def test_submit_is_idempotent(self) -> None:
        first = self.service.submit(self.payload())
        second = self.service.submit(self.payload())
        self.assertEqual(API_CONTRACT_VERSION, first["contract_version"])
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

    def test_completed_attempt_can_be_saved_to_archive(self) -> None:
        attempt = self.service.submit(self.payload())
        saved = self.service.archive(attempt["attempt_id"])
        self.assertIsNotNone(saved["archived_at"])
        archive = self.service.list_archive()
        self.assertEqual(1, len(archive))
        self.assertEqual(attempt["attempt_id"], archive[0]["attempt_id"])
        self.assertEqual(attempt["question"]["title"], archive[0]["title"])
        self.assertEqual(attempt["response"], archive[0]["response"])
        self.assertEqual(attempt["comparison"], archive[0]["comparison"])

    def test_saving_same_attempt_to_archive_is_idempotent(self) -> None:
        attempt = self.service.submit(self.payload())
        first = self.service.archive(attempt["attempt_id"])
        second = self.service.archive(attempt["attempt_id"])
        self.assertEqual(first["archived_at"], second["archived_at"])
        self.assertEqual(1, len(self.service.list_archive()))

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

        children = toggle["toggle"]["children"]
        headings = [
            child[child["type"]]["rich_text"][0]["text"]["content"]
            for child in children
            if child["type"].startswith("heading_")
        ]
        self.assertEqual(
            ["상황", "내 답변", "선택한 이유", "실제 답과 내 답변의 차이와 고려해볼 점"],
            headings,
        )
        reveal = next(child for child in children if child["type"] == "toggle")
        self.assertEqual(
            "실제 결과와 관점 전환 보기",
            reveal["toggle"]["rich_text"][0]["text"]["content"],
        )
        reveal_headings = [
            child[child["type"]]["rich_text"][0]["text"]["content"]
            for child in reveal["toggle"]["children"]
            if child["type"].startswith("heading_")
        ]
        self.assertEqual(
            ["실제 사례", "추천 판단", "관점 전환", "내 답과 비교하기", "다음에 가져갈 질문", "참조"],
            reveal_headings,
        )

    def test_notion_toggle_supports_attempts_without_new_template_fields(self) -> None:
        attempt = self.service.submit(self.payload())
        for field in (
            "recommended_option",
            "recommendation_reason",
            "perspective_before",
            "perspective_after",
        ):
            attempt["comparison"].pop(field, None)
        attempt["question"]["source"].pop("published_at", None)
        attempt["question"]["source"].pop("evidence_level", None)

        toggle = build_attempt_toggle(attempt)
        reveal = next(child for child in toggle["toggle"]["children"] if child["type"] == "toggle")
        serialized = json.dumps(reveal, ensure_ascii=False)
        self.assertIn("확인되지 않음", serialized)

    def test_notion_database_properties_match_newsletter_schema(self) -> None:
        attempt = self.service.submit(self.payload())
        expected_publisher = attempt["question"]["source"]["publisher"]
        expected_url = attempt["question"]["source"]["url"]
        attempt["question"]["source"].update(
            {
                "published_at": "2026-09-18",
                "evidence_level": "공식",
                "related_topics": ["행동 분석", "지표"],
            }
        )
        schema = {
            "자료명": {"type": "title"},
            "발행처": {"type": "rich_text"},
            "발행일": {"type": "date"},
            "원문": {"type": "url"},
            "읽음": {"type": "checkbox"},
            "근거 수준": {"type": "select"},
            "관련 주제": {"type": "multi_select"},
        }

        properties = build_attempt_database_properties(attempt, schema, "읽음")

        title = properties["자료명"]["title"][0]["text"]["content"]
        self.assertEqual(attempt["question"]["title"], title)
        self.assertNotIn(attempt["attempt_id"], title)
        self.assertNotIn(attempt["created_at"][:10], title)
        self.assertEqual(expected_publisher, properties["발행처"]["rich_text"][0]["text"]["content"])
        self.assertEqual("2026-09-18", properties["발행일"]["date"]["start"])
        self.assertEqual(expected_url, properties["원문"]["url"])
        self.assertTrue(properties["읽음"]["checkbox"])
        self.assertEqual("공식", properties["근거 수준"]["select"]["name"])
        self.assertEqual(
            [{"name": "행동 분석"}, {"name": "지표"}], properties["관련 주제"]["multi_select"]
        )

    def test_notion_resolves_inline_database_from_page(self) -> None:
        client = NotionClient("token", "2026-03-11")
        responses = {
            ("GET", "/blocks/page-id/children?page_size=100"): {
                "results": [
                    {"id": "database-id", "type": "child_database", "child_database": {}}
                ],
                "has_more": False,
            },
            ("GET", "/databases/database-id"): {
                "object": "database",
                "id": "database-id",
                "data_sources": [{"id": "source-id"}],
            },
            ("GET", "/data_sources/source-id"): {
                "object": "data_source",
                "id": "source-id",
                "properties": {"자료명": {"type": "title"}},
            },
        }

        def fake_request(method: str, path: str, payload=None):
            if path == "/databases/page-id":
                raise NotionError("page, not database", 400)
            return responses[(method, path)]

        client.request = fake_request
        target = client.resolve_data_source("page-id")
        self.assertEqual("database-id", target["database_id"])
        self.assertEqual("source-id", target["data_source_id"])


if __name__ == "__main__":
    unittest.main()
