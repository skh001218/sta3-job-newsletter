"""Research newsletter sources with Codex, merge validated questions, then notify Slack."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "newsletter_questions.schema.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ProgressReporter:
    def __init__(self, path: Path | None):
        self.path = path
        self.percent = 0

    def update(self, percent: int, stage: str, message: str) -> None:
        self.percent = max(self.percent, min(100, percent))
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "percent": self.percent,
                    "stage": stage,
                    "message": message,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def normalized_url(value: str) -> str:
    parts = urlsplit(value.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def safe_question_id(value: str, source_url: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not slug:
        slug = "newsletter-question"
    suffix = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:70]}-{suffix}"


def to_question(item: dict[str, Any]) -> dict[str, Any]:
    source = item["source"]
    source_url = normalized_url(source["url"])
    if urlsplit(source_url).scheme not in {"http", "https"}:
        raise ValueError("문제 원문 URL은 http 또는 https 주소여야 합니다.")
    option_ids = {option["id"] for option in item["options"]}
    evaluation_ids = {row["option_id"] for row in item["evaluation_by_option"]}
    if len(option_ids) != len(item["options"]) or evaluation_ids != option_ids:
        raise ValueError("모든 선택지 ID는 고유해야 하며 각각의 평가가 필요합니다.")
    if item["recommended_option"] not in option_ids:
        raise ValueError("추천 선택지는 실제 선택지 중 하나여야 합니다.")
    by_option = {
        row["option_id"]: {
            "well_considered": row["well_considered"],
            "missing_considerations": row["missing_considerations"],
            "reasonable_despite_outcome": row["reasonable_despite_outcome"],
        }
        for row in item["evaluation_by_option"]
    }
    return {
        "id": safe_question_id(item["id"], source_url),
        "version": 1,
        "active": True,
        "title": item["title"],
        "scenario": item["scenario"],
        "prompt": item["prompt"],
        "options": item["options"],
        "answer_fields": ["reason", "expected_outcome", "assumptions", "confidence"],
        "source": {
            "title": source["title"],
            "publisher": source["publisher"],
            "published_at": source["published_at"],
            "evidence_level": source["evidence_level"],
            "related_topics": source["related_topics"],
            "url": source_url,
            "notion_page_id": source["notion_page_id"],
        },
        "reveal": {
            "actual_action": item["actual_action"],
            "actual_outcome": item["actual_outcome"],
            "metrics": item["metrics"],
            "recommended_option": item["recommended_option"],
            "recommendation_reason": item["recommendation_reason"],
            "perspective_before": item["perspective_before"],
            "perspective_after": item["perspective_after"],
            "evidence_links": item["evidence_links"],
        },
        "evaluation": {
            "rubric_version": 1,
            "by_option": by_option,
            "unknown_from_evidence": item["unknown_from_evidence"],
            "next_questions": item["next_questions"],
        },
    }


def merge_questions(path: Path, generated: list[dict[str, Any]]) -> dict[str, int]:
    from app.questions import QuestionRepository

    payload = json.loads(path.read_text(encoding="utf-8"))
    existing = payload.get("questions", [])
    existing_urls = {
        normalized_url(question.get("source", {}).get("url", ""))
        for question in existing
        if question.get("source", {}).get("url")
    }
    existing_ids = {question.get("id") for question in existing}
    added: list[dict[str, Any]] = []
    skipped = 0
    for raw in generated:
        question = to_question(raw)
        url = normalized_url(question["source"]["url"])
        if url in existing_urls:
            skipped += 1
            continue
        while question["id"] in existing_ids:
            question["id"] += "-new"
        existing_urls.add(url)
        existing_ids.add(question["id"])
        added.append(question)

    if not added:
        return {"added_count": 0, "skipped_count": skipped}

    candidate = {"questions": existing + added}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", dir=path.parent, delete=False
    ) as temporary:
        json.dump(candidate, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        QuestionRepository(temporary_path).active()
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {"added_count": len(added), "skipped_count": skipped}


def run_codex(
    max_items: int, output_path: Path, reporter: ProgressReporter
) -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex CLI를 찾을 수 없습니다.")
    prompt = f"""
$game-da-newsletter-research를 사용해 최근 1년의 한국어 게임 데이터 분석 실무 자료를 최대 {max_items}개 조사하세요.
기존 Notion 데이터베이스는 조사 자료와 중복 판단의 참고로 읽고, data/questions.json에 같은 원문 URL의 문제가 있는 경우만 생성 대상에서 제외하세요.
Notion에 이미 있지만 data/questions.json에 없는 자료는 문제 생성 대상에 포함하세요.
이번 실행에서는 Notion을 수정하거나 Slack 메시지를 보내지 마세요. 문제 등록과 Slack 알림은 호출한 애플리케이션이 검증 후 처리합니다.
각 신규 자료로 실제 결과를 보기 전에 풀 수 있는 상황형 객관식 문제를 하나 생성하세요.
사실, 수치, 실제 조치와 결과는 반드시 원문에서 확인된 내용만 사용하고 확인되지 않은 내용은 명시하세요.
추천 선택지 위치를 고정하지 말고 모든 선택지에 대한 평가를 작성하세요.
소스 코드나 로컬 파일은 변경하지 말고, 요청된 JSON 스키마에 맞는 결과만 반환하세요.
""".strip()
    reporter.update(8, "PREPARING", "조사 조건과 기존 문제를 확인하는 중입니다.")
    process = subprocess.Popen(
        [
            executable,
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(SCHEMA_PATH),
            "--output-last-message",
            str(output_path),
            "--cd",
            str(ROOT),
            prompt,
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    output_tail: list[str] = []
    event_count = 0
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        output_tail.append(line)
        output_tail = output_tail[-20:]
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        event_type = str(event.get("type", ""))
        percent = min(74, 10 + event_count * 2)
        if event_type.endswith("started"):
            message = "최신 자료를 조사하고 있습니다."
        elif event_type.endswith("completed"):
            message = "수집한 자료를 검수하고 문제로 구성하는 중입니다."
        else:
            message = "자료 조사와 근거 검수를 진행하고 있습니다."
        reporter.update(percent, "RESEARCHING", message)
    try:
        return_code = process.wait(timeout=1500)
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError("Codex 조사가 제한 시간 안에 끝나지 않았습니다.")
    if return_code:
        detail = "\n".join(output_tail) or "Codex 조사 실패"
        raise RuntimeError(detail[-2000:])
    reporter.update(78, "GENERATING", "생성된 문제 형식을 확인하는 중입니다.")
    try:
        result = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex가 생성한 문제 JSON을 읽지 못했습니다.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("questions"), list):
        raise RuntimeError("Codex 문제 결과 형식이 올바르지 않습니다.")
    return result


def notify_slack() -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "send_slack_dm.py"),
            "--daily-after",
            "00:00",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or "Slack 알림 실패").strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="뉴스레터 조사, 문제 생성, 등록, 알림 워크플로")
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument("--questions", type=Path, default=ROOT / "data" / "questions.json")
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    reporter = ProgressReporter(args.progress)
    try:
        reporter.update(3, "STARTING", "뉴스레터 워크플로를 준비하고 있습니다.")
        with tempfile.TemporaryDirectory() as directory:
            result = run_codex(
                max(1, min(10, args.max_items)),
                Path(directory) / "result.json",
                reporter,
            )
        reporter.update(86, "VALIDATING", "문제 내용과 중복 URL을 검증하는 중입니다.")
        counts = merge_questions(args.questions.resolve(), result["questions"])
        reporter.update(94, "NOTIFYING", "문제 등록을 마치고 Slack 알림을 보내는 중입니다.")
        notify_slack()
        reporter.update(100, "COMPLETED", f"신규 문제 {counts['added_count']}개를 등록했습니다.")
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    except Exception as exc:
        reporter.update(reporter.percent, "FAILED", "실행 중 오류가 발생했습니다.")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
