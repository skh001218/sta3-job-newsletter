"""Send a Slack direct message with a bot token loaded from a local .env file."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=".env의 Slack 봇 토큰으로 개인 메시지를 보냅니다.")
    parser.add_argument("--user-id", help="받는 사람의 Slack 사용자 ID")
    parser.add_argument("--message", required=True, help="보낼 메시지")
    args = parser.parse_args()

    try:
        load_env(ENV_PATH)
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        recipient = (args.user_id or os.environ.get("SLACK_USER_ID", "")).strip()
        if not token:
            raise RuntimeError(".env에 SLACK_BOT_TOKEN 값이 필요합니다.")
        if not recipient:
            raise RuntimeError("--user-id 또는 .env의 SLACK_USER_ID 값이 필요합니다.")

        result = post_message(token, recipient, args.message)
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
