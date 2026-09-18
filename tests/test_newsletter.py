from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.newsletter import NewsletterWorkflow
from app.questions import QuestionRepository
from scripts.run_newsletter_workflow import ProgressReporter, merge_questions


ROOT = Path(__file__).resolve().parent.parent


def generated_question(url: str = "https://example.org/case") -> dict:
    return {
        "id": "live-ops-case",
        "title": "이벤트 이탈 원인 판단",
        "scenario": "이벤트 중반 이탈이 증가했다.",
        "prompt": "가장 먼저 확인할 것은?",
        "options": [
            {"id": "A", "label": "보상을 늘린다."},
            {"id": "B", "label": "구간별 이탈을 확인한다."},
        ],
        "source": {
            "title": "실무 사례",
            "publisher": "테스트 발행처",
            "published_at": "2026-09-18",
            "evidence_level": "공식",
            "related_topics": ["행동 분석"],
            "url": url,
            "notion_page_id": "",
        },
        "actual_action": "구간별 이탈을 확인했다.",
        "actual_outcome": "병목 구간을 개선했다.",
        "metrics": ["완주율 상승"],
        "recommended_option": "B",
        "recommendation_reason": "개입 전에 원인을 확인한다.",
        "perspective_before": "결과 지표에 바로 개입한다.",
        "perspective_after": "원인 구간을 확인하고 실험한다.",
        "evidence_links": [{"label": "원문", "url": url}],
        "evaluation_by_option": [
            {
                "option_id": "A",
                "well_considered": ["참여 동기를 고려했다."],
                "missing_considerations": ["원인이 확인되지 않았다."],
                "reasonable_despite_outcome": ["보상 민감도가 확인됐다면 가능하다."],
            },
            {
                "option_id": "B",
                "well_considered": ["원인을 먼저 확인했다."],
                "missing_considerations": ["노출 편향도 확인해야 한다."],
                "reasonable_despite_outcome": ["근거에 가장 가깝다."],
            },
        ],
        "unknown_from_evidence": ["표본 규모"],
        "next_questions": ["다른 코호트도 같은가?"],
    }


class NewsletterWorkflowTest(unittest.TestCase):
    def settings(self, directory: str) -> Settings:
        return Settings(
            host="127.0.0.1",
            port=8000,
            access_token="",
            db_path=Path(directory) / "app.db",
            question_data_path=Path(directory) / "questions.json",
            notion_token="",
            notion_page_id="",
            notion_read_property="읽음",
            notion_api_version="2026-03-11",
            sync_interval_seconds=10,
            max_retries=2,
            newsletter_auto_enabled=True,
            newsletter_run_at="08:00",
            newsletter_state_path=Path(directory) / "state.json",
        )

    def test_workflow_is_due_only_after_schedule_and_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflow = NewsletterWorkflow(self.settings(directory), runner=lambda: {})
            self.assertFalse(workflow.due(datetime.fromisoformat("2026-09-18T07:59:00+09:00")))
            self.assertTrue(workflow.due(datetime.fromisoformat("2026-09-18T08:00:00+09:00")))
            Path(directory, "state.json").write_text(
                json.dumps({"last_attempt_date": "2026-09-18"}), encoding="utf-8"
            )
            self.assertFalse(workflow.due(datetime.fromisoformat("2026-09-18T09:00:00+09:00")))

    def test_progress_reporter_is_monotonic_and_exposed_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            progress_path = Path(directory) / "state_progress.json"
            reporter = ProgressReporter(progress_path)
            reporter.update(30, "RESEARCHING", "자료 조사 중")
            reporter.update(10, "RESEARCHING", "근거 검수 중")
            status = NewsletterWorkflow(settings, runner=lambda: {}).status()
            self.assertEqual(30, status["progress_percent"])
            self.assertEqual("근거 검수 중", status["progress_message"])

    def test_generated_questions_are_validated_and_deduplicated_by_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps({"questions": []}), encoding="utf-8")
            first = merge_questions(path, [generated_question()])
            second = merge_questions(path, [generated_question("https://example.org/case/")])
            self.assertEqual({"added_count": 1, "skipped_count": 0}, first)
            self.assertEqual({"added_count": 0, "skipped_count": 1}, second)
            self.assertEqual(1, len(QuestionRepository(path).active()))

    def test_repository_exposes_question_summaries_without_answers(self) -> None:
        summaries = QuestionRepository(ROOT / "data" / "questions.json").summaries()
        self.assertTrue(summaries)
        self.assertNotIn("reveal", summaries[0])
        self.assertIn("publisher", summaries[0])


if __name__ == "__main__":
    unittest.main()
