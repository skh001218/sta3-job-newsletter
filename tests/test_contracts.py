from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.contracts import API_CONTRACT_VERSION, AttemptStatus, PersistenceStatus
from app.questions import QuestionError, QuestionRepository


ROOT = Path(__file__).resolve().parent.parent


class ContractTest(unittest.TestCase):
    def test_status_values_are_stable_api_values(self) -> None:
        self.assertEqual("SUBMITTED", AttemptStatus.SUBMITTED)
        self.assertEqual("COMPARISON_READY", AttemptStatus.COMPARISON_READY)
        self.assertEqual("NOT_REQUESTED", PersistenceStatus.NOT_REQUESTED)
        self.assertEqual("SAVED", PersistenceStatus.SAVED)

    def test_public_question_declares_contract_version(self) -> None:
        question = QuestionRepository(ROOT / "data" / "questions.json").current()
        public = QuestionRepository.public_view(question)
        self.assertEqual(API_CONTRACT_VERSION, public["contract_version"])
        self.assertNotIn("reveal", public)
        self.assertNotIn("evaluation", public)

    def test_question_version_must_be_positive_integer(self) -> None:
        source = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))
        source["questions"][0]["version"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(QuestionError, "1 이상의 정수"):
                QuestionRepository(path).current()

    def test_question_ids_must_be_unique(self) -> None:
        source = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))
        source["questions"].append(dict(source["questions"][0]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(QuestionError, "고유"):
                QuestionRepository(path).current()


if __name__ == "__main__":
    unittest.main()
