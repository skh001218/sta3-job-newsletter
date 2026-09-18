"""Send a Slack direct message with a bot token loaded from a local .env file."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
DAILY_STATE_PATH = PROJECT_ROOT / "data" / "slack_daily_state.json"


def load_env(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"설정 파일을 찾을 수 없습니다: {path}")

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def call_slack_api(token: str, method: str, payload_data: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"Slack 연결에 실패했습니다: {error.reason}") from error

    if not result.get("ok"):
        raise RuntimeError(f"Slack API 요청에 실패했습니다({method}): {result.get('error', 'unknown_error')}")
    return result


def post_message(token: str, recipient: str, message: str) -> dict[str, object]:
    channel = recipient
    if recipient.startswith("U"):
        opened = call_slack_api(token, "conversations.open", {"users": recipient})
        channel_data = opened.get("channel")
        if not isinstance(channel_data, dict) or not channel_data.get("id"):
            raise RuntimeError("Slack이 개인 대화 채널 ID를 반환하지 않았습니다.")
        channel = str(channel_data["id"])

    return call_slack_api(token, "chat.postMessage", {"channel": channel, "text": message})


def load_daily_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_daily_state(path: Path, sent_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({"last_sent_date": sent_date}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=".env의 Slack 봇 토큰으로 개인 메시지를 보냅니다.")
    parser.add_argument("--user-id", help="받는 사람의 Slack 사용자 ID")
    parser.add_argument("--message", help="보낼 메시지")
    parser.add_argument(
        "--daily-after",
        metavar="HH:MM",
        help="현지 시각 이후 하루 한 번 정해진 안내 메시지를 보내고 실패 시 다음 실행에서 재시도",
    )
    args = parser.parse_args()

    try:
        load_env(ENV_PATH)
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        recipient = (args.user_id or os.environ.get("SLACK_USER_ID", "")).strip()
        if not token:
            raise RuntimeError(".env에 SLACK_BOT_TOKEN 값이 필요합니다.")
        if not recipient:
            raise RuntimeError("--user-id 또는 .env의 SLACK_USER_ID 값이 필요합니다.")

        message = args.message
        sent_date: str | None = None
        if args.daily_after:
            try:
                send_after = dt.time.fromisoformat(args.daily_after)
            except ValueError as error:
                raise RuntimeError("--daily-after 값은 HH:MM 형식이어야 합니다.") from error

            now = dt.datetime.now().astimezone()
            sent_date = now.date().isoformat()
            if now.timetz().replace(tzinfo=None) < send_after:
                print(json.dumps({"ok": True, "skipped": "before_send_time"}, ensure_ascii=False))
                return 0

            state = load_daily_state(DAILY_STATE_PATH)
            if state.get("last_sent_date") == sent_date:
                print(json.dumps({"ok": True, "skipped": "already_sent_today"}, ensure_ascii=False))
                return 0

            message = f"{sent_date}자 사고력 키우기\nhttp://127.0.0.1:8000"

        if not message:
            raise RuntimeError("--message 또는 --daily-after 값이 필요합니다.")

        result = post_message(token, recipient, message)
        if sent_date:
            save_daily_state(DAILY_STATE_PATH, sent_date)
        safe_result = {
            "ok": True,
            "channel": result.get("channel"),
            "ts": result.get("ts"),
        }
        print(json.dumps(safe_result, ensure_ascii=False))
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
