from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, Settings


class NewsletterWorkflow:
    """예약 시각에 검증된 조사 명령을 실행하고 최근 상태를 보존한다."""

    def __init__(
        self,
        settings: Settings,
        runner: Callable[[], dict[str, Any]] | None = None,
    ):
        self.settings = settings
        self._runner = runner or self._run_command
        self._lock = threading.Lock()
        self._running = False
        self._progress_path = settings.newsletter_state_path.with_name(
            f"{settings.newsletter_state_path.stem}_progress.json"
        )

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        last_status = state.get("last_status", "NEVER")
        last_error = state.get("last_error")
        if last_status == "RUNNING" and not self._running:
            last_status = "INTERRUPTED"
            last_error = last_error or "서버가 다시 시작되어 이전 실행이 중단되었습니다."
        progress = self._read_json(self._progress_path)
        default_percent = 100 if last_status == "SUCCEEDED" else 0
        default_stage = "COMPLETED" if last_status == "SUCCEEDED" else "IDLE"
        default_message = (
            f"신규 문제 {int(state.get('added_count', 0))}개 등록을 완료했습니다."
            if last_status == "SUCCEEDED"
            else "대기 중"
        )
        return {
            "enabled": self.settings.newsletter_auto_enabled,
            "run_at": self.settings.newsletter_run_at,
            "max_items": self.settings.newsletter_max_items,
            "running": self._running,
            "last_started_at": state.get("last_started_at"),
            "last_finished_at": state.get("last_finished_at"),
            "last_attempt_date": state.get("last_attempt_date"),
            "last_success_date": state.get("last_success_date"),
            "last_status": last_status,
            "added_count": state.get("added_count", 0),
            "skipped_count": state.get("skipped_count", 0),
            "last_error": last_error,
            "progress_percent": int(progress.get("percent", default_percent)),
            "progress_stage": progress.get("stage", default_stage),
            "progress_message": progress.get("message", default_message),
        }

    def due(self, now: datetime | None = None) -> bool:
        if not self.settings.newsletter_auto_enabled:
            return False
        current = now or datetime.now().astimezone()
        try:
            run_at = time.fromisoformat(self.settings.newsletter_run_at)
        except ValueError:
            return False
        state = self._read_state()
        attempted_date = (
            state.get("last_attempt_date")
            or state.get("last_success_date")
            or str(state.get("last_started_at", ""))[:10]
        )
        return (
            current.timetz().replace(tzinfo=None) >= run_at
            and attempted_date != current.date().isoformat()
            and not self._running
        )

    def run_if_due(self) -> bool:
        return self.run_async() if self.due() else False

    def run_async(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._execute, daemon=True, name="newsletter-workflow").start()
        return True

    def _execute(self) -> None:
        started = datetime.now().astimezone()
        self._write_progress(2, "STARTING", "워크플로를 시작하는 중입니다.")
        self._write_state(
            {
                **self._read_state(),
                "last_started_at": started.isoformat(timespec="seconds"),
                "last_attempt_date": started.date().isoformat(),
                "last_status": "RUNNING",
                "last_error": None,
            }
        )
        try:
            result = self._runner()
            finished = datetime.now().astimezone()
            self._write_state(
                {
                    "last_started_at": started.isoformat(timespec="seconds"),
                    "last_attempt_date": started.date().isoformat(),
                    "last_finished_at": finished.isoformat(timespec="seconds"),
                    "last_success_date": finished.date().isoformat(),
                    "last_status": "SUCCEEDED",
                    "added_count": int(result.get("added_count", 0)),
                    "skipped_count": int(result.get("skipped_count", 0)),
                    "last_error": None,
                }
            )
            self._write_progress(
                100,
                "COMPLETED",
                f"신규 문제 {int(result.get('added_count', 0))}개 등록을 완료했습니다.",
            )
        except Exception as exc:
            finished = datetime.now().astimezone()
            self._write_state(
                {
                    **self._read_state(),
                    "last_finished_at": finished.isoformat(timespec="seconds"),
                    "last_status": "FAILED",
                    "last_error": str(exc)[:1000],
                }
            )
            current = self._read_json(self._progress_path)
            self._write_progress(
                int(current.get("percent", 0)),
                "FAILED",
                "실행 중 오류가 발생했습니다.",
            )
        finally:
            with self._lock:
                self._running = False

    def _run_command(self) -> dict[str, Any]:
        script = ROOT / "scripts" / "run_newsletter_workflow.py"
        process = subprocess.run(
            [
                sys.executable,
                str(script),
                "--max-items",
                str(self.settings.newsletter_max_items),
                "--questions",
                str(self.settings.question_data_path),
                "--progress",
                str(self._progress_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1800,
            check=False,
        )
        if process.returncode:
            detail = (process.stderr or process.stdout or "워크플로 실행 실패").strip()
            raise RuntimeError(detail[-1000:])
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("워크플로 결과를 JSON으로 읽지 못했습니다.") from exc
        if not isinstance(result, dict):
            raise RuntimeError("워크플로 결과 형식이 올바르지 않습니다.")
        return result

    def _read_state(self) -> dict[str, Any]:
        return self._read_json(self.settings.newsletter_state_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_progress(self, percent: int, stage: str, message: str) -> None:
        path = self._progress_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "percent": max(0, min(100, percent)),
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
        temporary.replace(path)

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self.settings.newsletter_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


class NewsletterWorkflowScheduler(threading.Thread):
    def __init__(self, workflow: NewsletterWorkflow, interval_seconds: int = 30):
        super().__init__(daemon=True, name="newsletter-scheduler")
        self.workflow = workflow
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.workflow.run_if_due()
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
