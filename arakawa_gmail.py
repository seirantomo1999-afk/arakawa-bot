# arakawa_gmail.py
"""
荒川区テニスコート予約bot - Gmail 通知
要件定義 REQUIREMENTS.md §6: 予約成功時は必ず通知する
- 空き枠検出時: 空き状況を通知
- 予約完了時: 予約内容（日時・コート）を通知
"""
from __future__ import annotations

import base64
import re
import subprocess
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from arakawa_calendar import GMAIL_SCOPES, get_google_creds

TO_EMAIL = "seirantomo1999@gmail.com"


def get_service():
    """arakawa_calendar の token.json / credentials.json を使い Gmail サービスを取得"""
    creds = get_google_creds(GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds)

def create_message(to: str, subject: str, body_text: str) -> dict:
    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}

def send_message(service, user_id: str, message: dict):
    return service.users().messages().send(userId=user_id, body=message).execute()

if __name__ == "__main__":
    print("Running:", __file__)
    service = get_service()

    # ===== スクレイピングの結果を本文にする =====
    result = subprocess.run(
        ["python", r"arakawa_selenium_check.py"],
        capture_output=True, text=True, timeout=900
    )
    raw_out = (result.stdout or "").splitlines()
    raw_err = (result.stderr or "").strip()

    # --- ② 「該当なし」行とその1つ上の行も除外 ---
    DROP_KEYWORDS = ("該当なし", "空きなし", "A_で始まるセルは見つかりません")

    filtered = []
    for ln in raw_out:
        # [数字] で始まる行だけを残す 例: [1] のような形式
        if re.match(r"^\[\d+\]", ln.strip()):
            filtered.append(ln)

    body = "\n".join(filtered).strip()

    # --- ① [数字]で始まる行が1つもなければ送信しない ---
    has_hit = any(
        re.match(r"\[\d+\]", ln.strip())
        for ln in filtered
    )

    # スクレイパ異常終了時はエラーメールに切り替え（任意）
    if result.returncode != 0:
        subject = "【エラー】荒川区テニスコートスクレイピング失敗"
        body = (body + "\n\n--- エラー出力 ---\n" + (raw_err or "(なし)")).strip()
        msg = create_message(to=TO_EMAIL, subject=subject, body_text=body or "(本文なし)")
        resp = send_message(service, "me", msg)
        print("Sent (error report):", resp.get("id"))
        sys.exit(0)

    # 予約完了通知（BOOKED: で始まる行を解析）
    booked_lines = [ln.strip().replace("BOOKED:", "", 1).strip() for ln in raw_out if "BOOKED:" in ln]
    if booked_lines:
        subject = "【自動通知】荒川区テニスコート 予約が完了しました"
        body_parts = ["以下の枠で予約が完了しました。", ""]
        for i, line in enumerate(booked_lines, 1):
            body_parts.append(f"{i}. {line}")
        body_parts.extend(["", "※キャンセルが必要な場合は手動で区のサイトから行ってください。"])
        msg = create_message(to=TO_EMAIL, subject=subject, body_text="\n".join(body_parts))
        resp = send_message(service, "me", msg)
        print("Sent (予約完了通知):", resp.get("id"))
        sys.exit(0)

    # 空きヒットが無ければ送らず終了
    if not has_hit:
        print("空きが見つからなかったため、メール送信をスキップしました。")
        sys.exit(0)

    # 空きあり → 空き状況通知
    subject = "【自動通知】荒川区テニスコート 休日空き状況"
    msg = create_message(to=TO_EMAIL, subject=subject, body_text=body or "(本文なし)")
    resp = send_message(service, "me", msg)
    print("Sent:", resp.get("id"))
