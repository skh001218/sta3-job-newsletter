from __future__ import annotations

import hmac
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import Settings
from .contracts import PersistenceStatus
from .db import Database
from .notion import NotionSyncService, NotionSyncWorker
from .questions import QuestionRepository
from .service import AttemptService, ServiceError


STATIC_DIR = Path(__file__).resolve().parent / "static"
ATTEMPT_ROUTE = re.compile(r"^/api/attempts/([0-9a-f-]+)$")
COMPARISON_RETRY_ROUTE = re.compile(r"^/api/attempts/([0-9a-f-]+)/comparison/retry$")
COMPLETE_ROUTE = re.compile(r"^/api/attempts/([0-9a-f-]+)/complete$")
NOTION_RETRY_ROUTE = re.compile(r"^/api/attempts/([0-9a-f-]+)/notion/retry$")
QUESTION_ROUTE = re.compile(r"^/api/questions/([^/]+)$")


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.initialize()
        questions = QuestionRepository(settings.question_data_path)
        self.attempts = AttemptService(self.db, questions)
        self.notion = NotionSyncService(self.db, self.attempts, settings)
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # 답변 본문이 서버 로그에 남지 않도록 요청 메타데이터만 기록한다.
        super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if path != "/api/health" and not self._authorized():
                return self._json({"error": "API 접근 토큰이 필요합니다."}, HTTPStatus.UNAUTHORIZED)
            try:
                if path == "/api/health":
                    return self._json({"ok": True})
                if path == "/api/questions/current":
                    return self._json(self.server.attempts.get_public_question())
                match = QUESTION_ROUTE.match(path)
                if match:
                    return self._json(self.server.attempts.get_public_question(unquote(match.group(1))))
                match = ATTEMPT_ROUTE.match(path)
                if match:
                    return self._json(self.server.attempts.get(match.group(1)))
                return self._json({"error": "API 경로를 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
            except ServiceError as exc:
                return self._json({"error": str(exc)}, exc.status)
            except Exception:
                return self._json({"error": "서버 내부 오류가 발생했습니다."}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._json({"error": "경로를 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
        if not self._authorized():
            return self._json({"error": "API 접근 토큰이 필요합니다."}, HTTPStatus.UNAUTHORIZED)
        try:
            payload = self._read_json()
            if path == "/api/attempts":
                return self._json(self.server.attempts.submit(payload), HTTPStatus.CREATED)
            match = COMPARISON_RETRY_ROUTE.match(path)
            if match:
                return self._json(self.server.attempts.retry_comparison(match.group(1)))
            match = COMPLETE_ROUTE.match(path)
            if match:
                key = payload.get("idempotency_key")
                if not isinstance(key, str) or not key.strip():
                    raise ServiceError("idempotency_key가 필요합니다.")
                result = self.server.notion.enqueue(match.group(1), key.strip())
                status = (
                    HTTPStatus.OK
                    if result["notion_status"] == PersistenceStatus.SAVED
                    else HTTPStatus.ACCEPTED
                )
                return self._json(result, status)
            match = NOTION_RETRY_ROUTE.match(path)
            if match:
                result = self.server.notion.retry(match.group(1))
                status = (
                    HTTPStatus.OK
                    if result["notion_status"] == PersistenceStatus.SAVED
                    else HTTPStatus.ACCEPTED
                )
                return self._json(result, status)
            return self._json({"error": "API 경로를 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
        except ServiceError as exc:
            return self._json({"error": str(exc)}, exc.status)
        except ValueError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            return self._json({"error": "서버 내부 오류가 발생했습니다."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _authorized(self) -> bool:
        expected = self.server.settings.access_token
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {expected}")

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type은 application/json이어야 합니다.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("잘못된 Content-Length입니다.") from exc
        if length <= 0 or length > 65536:
            raise ValueError("요청 크기는 1~65536바이트여야 합니다.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("올바른 JSON 요청이 아닙니다.") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON 객체가 필요합니다.")
        return value

    def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        files = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }
        filename = files.get(path)
        if not filename:
            return self._json({"error": "페이지를 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
        file_path = STATIC_DIR / filename
        body = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def run() -> None:
    settings = Settings.from_env()
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and not settings.access_token:
        raise SystemExit("APP_HOST를 외부에 공개할 때는 APP_ACCESS_TOKEN을 설정해야 합니다.")
    server = AppServer((settings.host, settings.port), settings)
    worker = NotionSyncWorker(server.notion, settings.sync_interval_seconds)
    worker.start()
    print(f"게임 DA 문제 풀이: http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.shutdown()
        server.server_close()
