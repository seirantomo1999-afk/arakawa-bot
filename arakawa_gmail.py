"""Run the scraper, notify its results, and preserve failures for Actions."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import subprocess
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from arakawa_calendar import GMAIL_SCOPES, get_google_creds
from arakawa_config import SCRAPER_TIMEOUT_SECONDS, TO_EMAIL

SCRIPT_DIR = Path(__file__).resolve().parent


def get_service():
    return build("gmail", "v1", credentials=get_google_creds(GMAIL_SCOPES))


def create_message(to: str, subject: str, body_text: str) -> dict:
    msg = MIMEMultipart()
    msg["To"], msg["Subject"] = to, subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")}


def send_message(service, user_id: str, message: dict):
    return service.users().messages().send(userId=user_id, body=message).execute()


def _text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _redact(text: str) -> str:
    for key in ("ARAKAWA_USER_ID", "ARAKAWA_PASSWORD"):
        value = os.getenv(key)
        if value:
            text = text.replace(value, "[redacted]")
    return text


def run_scraper():
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "arakawa_selenium_check.py")],
            cwd=SCRIPT_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SCRAPER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            exc.cmd, 124, _text(exc.stdout),
            _text(exc.stderr) + "\n巡回がタイムアウトしました。予約一覧を確認してください。",
        )


def messages_for_result(result):
    lines = _text(result.stdout).splitlines()
    booked = list(dict.fromkeys(line.removeprefix("BOOKED:").strip()
                                for line in lines if line.startswith("BOOKED:")))
    hard = list(dict.fromkeys(line.removeprefix("DISPLAY_ONLY:").strip()
                              for line in lines if line.startswith("DISPLAY_ONLY:")))
    hits = list(dict.fromkeys(line.strip() for line in lines if re.match(r"^\[\d+\]", line.strip())))
    messages = []
    # Send already completed bookings even when a later page fails.
    if booked:
        body = "以下の枠で予約が完了しました。\n\n" + "\n".join(
            f"{i}. {line}" for i, line in enumerate(booked, 1)
        )
        if hard:
            body += "\n\n--- 自動予約対象外（ハードコート）の空き ---\n" + "\n".join(hard)
        body += "\n\n※キャンセルは手動で区のサイトから行ってください。"
        messages.append(("【自動通知】荒川区テニスコート 予約が完了しました", body))
    elif result.returncode == 0 and hits:
        messages.append(("【自動通知】荒川区テニスコート 休日空き状況", "\n".join(hits)))
    if result.returncode != 0:
        body = f"巡回に失敗しました（終了コード: {result.returncode}）。\n予約操作の途中だった場合は区の予約一覧を確認してください。"
        body += "\n\n--- stderr ---\n" + _text(result.stderr)[-5000:]
        messages.append(("【エラー】荒川区テニスコートスクレイピング失敗", _redact(body)))
    return messages


def main() -> int:
    # Check mail authentication before attempting any reservation.
    service = get_service()
    result = run_scraper()
    print(f"Scraper exit code: {result.returncode}", flush=True)
    if result.returncode:
        print(_redact(_text(result.stderr))[-5000:], file=sys.stderr, flush=True)
    messages = messages_for_result(result)
    failed_notification = False
    for subject, body in messages:
        try:
            send_message(service, "me", create_message(TO_EMAIL, subject, body))
            print(f"Sent: {subject}", flush=True)
        except Exception as exc:
            # Do not print OAuth objects or complete API responses.
            print(f"Notification failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            failed_notification = True
    if not messages:
        print("空きが見つからなかったため、メール送信をスキップしました。", flush=True)
    return 1 if result.returncode or failed_notification else 0


if __name__ == "__main__":
    sys.exit(main())

