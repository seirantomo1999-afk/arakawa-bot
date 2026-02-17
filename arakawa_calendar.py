# -*- coding: utf-8 -*-
"""
荒川区テニスコート予約bot - Google Calendar 連携
要件定義 REQUIREMENTS.md 第4節 に準拠
- 予約枠の前後2時間に予定があれば予約不可
- 1回の実行で対象期間の予定をまとめて取得しキャッシュ

認証: token.json を Credentials.from_authorized_user_file() で読み、
      Calendar 用スコープを付けて使用
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, time, timezone
from typing import TYPE_CHECKING

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

if TYPE_CHECKING:
    from arakawa_selenium_check import SlotInfo

# このスクリプトと同じディレクトリの token.json / credentials.json を参照
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(_SCRIPT_DIR, "token.json")
CREDENTIALS_PATH = os.path.join(_SCRIPT_DIR, "credentials.json")

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
# 1つの token.json でカレンダー・Gmail 両方を使うための統合スコープ
COMBINED_SCOPES = CALENDAR_SCOPES + GMAIL_SCOPES

# 予約枠の前後何時間を「予定があれば予約不可」とするか
HOURS_BUFFER = 2

def get_google_creds(scopes: list[str]) -> Credentials:
    """
    token.json から Credentials を取得。
    期限切れの場合は refresh して token.json を更新。
    初回認証時は COMBINED_SCOPES で発行し、カレンダー・Gmail 両方で使えるようにする。
    """
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, COMBINED_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"credentials.json が見つかりません: {CREDENTIALS_PATH}\n"
                    "初回認証時は credentials.json が必要です。"
                )
            with open(CREDENTIALS_PATH, "r", encoding="utf-8-sig") as f:
                raw = f.read()
            if not raw.strip():
                raise FileNotFoundError(
                    f"credentials.json が空です: {CREDENTIALS_PATH}\n"
                    "Google Cloud Console から OAuth クライアントの JSON をダウンロードして配置してください。"
                )
            client_config = json.loads(raw)
            flow = InstalledAppFlow.from_client_config(client_config, COMBINED_SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
    return creds


def get_calendar_service():
    """Calendar API のサービスを取得。token.json で認証。"""
    creds = get_google_creds(CALENDAR_SCOPES)
    return build("calendar", "v3", credentials=creds)


def fetch_calendar_busy_ranges(creds: Credentials) -> list[tuple[datetime, datetime]]:
    """
    primary カレンダーの now 〜 now+3ヶ月 の予定を取得し、
    busy_ranges = [(start_dt, end_dt), ...] に正規化する。
    終日予定はその日 00:00〜23:59:59 を busy とする。
    （検索結果の日付範囲に合わせて3ヶ月取得）
    """
    service = build("calendar", "v3", credentials=creds)
    tz = timezone.utc
    now = datetime.now(tz)
    time_max = now + timedelta(days=400)  # 検索結果が翌年まで出る場合もカバー
    time_min_str = now.isoformat()
    time_max_str = time_max.isoformat()

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min_str,
            timeMax=time_max_str,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    tz_jst = timezone(timedelta(hours=9))
    events = events_result.get("items", [])
    busy: list[tuple[datetime, datetime]] = []
    for e in events:
        start = e.get("start") or {}
        end = e.get("end") or {}
        if "dateTime" in start:
            start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
        else:
            # 終日: Google API の end.date は exclusive（その日は含まない）
            # 例: 3/10のみ → start=3/10, end=3/11 → 3/10 00:00〜3/10 23:59 にすべき
            date_str = start.get("date", "")
            end_date_str = end.get("date", date_str)
            if not date_str:
                continue
            try:
                d = date.fromisoformat(date_str)
                end_d = date.fromisoformat(end_date_str) if end_date_str else d
            except ValueError:
                continue
            start_dt = datetime.combine(d, time(0, 0, 0), tzinfo=tz_jst)
            last_day = end_d - timedelta(days=1)  # exclusive なので1日戻す
            end_dt = datetime.combine(last_day, time(23, 59, 59), tzinfo=tz_jst)
        busy.append((start_dt, end_dt))
    return busy


def fetch_events_in_range(service, start_date: date, end_date: date) -> list[tuple[datetime, datetime]]:
    """
    指定期間の予定を取得（fetch_calendar_busy_ranges の date 版）。
    service を渡す場合はこちらを使用。
    """
    tz = timezone.utc
    time_min = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=tz)
    time_max = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)
    time_max += timedelta(days=1)
    time_min_str = time_min.isoformat()
    time_max_str = time_max.isoformat()

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min_str,
            timeMax=time_max_str,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    tz_jst = timezone(timedelta(hours=9))
    result: list[tuple[datetime, datetime]] = []
    for item in events_result.get("items", []):
        start = item.get("start") or {}
        end = item.get("end") or {}
        if "dateTime" in start:
            start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
        else:
            # 終日: Google API の end.date は exclusive（その日は含まない）
            date_str = start.get("date", "")
            end_date_str = end.get("date", date_str)
            if not date_str:
                continue
            try:
                d = date.fromisoformat(date_str)
                end_d = date.fromisoformat(end_date_str) if end_date_str else d
            except ValueError:
                continue
            start_dt = datetime.combine(d, time(0, 0, 0), tzinfo=tz_jst)
            last_day = end_d - timedelta(days=1)
            end_dt = datetime.combine(last_day, time(23, 59, 59), tzinfo=tz_jst)
        result.append((start_dt, end_dt))
    return result


def _parse_time_to_minutes(t: str) -> int:
    """'09:00' -> 540（分）"""
    parts = t.strip().split(":")
    if len(parts) != 2:
        return 0
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return 0


def has_calendar_conflict(slot: "SlotInfo", events: list[tuple[datetime, datetime]], tz_offset_hours: int = 9) -> bool:
    """
    予約枠の前後2時間に予定が1件でもあれば True（予約不可）。
    例: 13:00-15:00 の枠 → 11:00-17:00 に予定があれば True
    """
    if not slot.start_time or not slot.end_time:
        return False  # 時間が不明な枠は競合なし扱い

    start_mins = _parse_time_to_minutes(slot.start_time)
    end_mins = _parse_time_to_minutes(slot.end_time)
    if start_mins == 0 and end_mins == 0:
        return False

    # チェック範囲: 開始の2時間前 ～ 終了の2時間後
    check_start_mins = max(0, start_mins - HOURS_BUFFER * 60)
    check_end_mins = min(24 * 60 - 1, end_mins + HOURS_BUFFER * 60)

    check_start_dt = datetime(
        slot.date_obj.year,
        slot.date_obj.month,
        slot.date_obj.day,
        check_start_mins // 60,
        check_start_mins % 60,
        0,
    )
    check_end_dt = datetime(
        slot.date_obj.year,
        slot.date_obj.month,
        slot.date_obj.day,
        check_end_mins // 60,
        check_end_mins % 60,
        59,
    )

    # ローカル日時を UTC で比較するためオフセットを適用（簡易: JST = UTC+9）
    jst = timezone(timedelta(hours=tz_offset_hours))
    check_start_utc = check_start_dt.replace(tzinfo=jst).astimezone(timezone.utc)
    check_end_utc = check_end_dt.replace(tzinfo=jst).astimezone(timezone.utc)

    for ev_start, ev_end in events:
        ev_start_utc = ev_start.astimezone(timezone.utc) if ev_start.tzinfo else ev_start.replace(tzinfo=timezone.utc)
        ev_end_utc = ev_end.astimezone(timezone.utc) if ev_end.tzinfo else ev_end.replace(tzinfo=timezone.utc)
        overlaps = ev_start_utc < check_end_utc and ev_end_utc > check_start_utc
        if overlaps:
            return True
    return False


def filter_by_calendar(
    candidates: list["SlotInfo"],
    service=None,
    events_cache: list[tuple[datetime, datetime]] | None = None,
):
    """
    カレンダーに予定がある枠を除外する。
    events_cache を渡すと再取得せずローカル判定のみ（1回取得・キャッシュ運用向け）。
    """
    if not candidates:
        return []

    if events_cache is not None:
        return [s for s in candidates if not has_calendar_conflict(s, events_cache)]

    if service is None:
        service = get_calendar_service()
    min_date = min(s.date_obj for s in candidates)
    max_date = max(s.date_obj for s in candidates)
    events = fetch_events_in_range(service, min_date, max_date)
    return [s for s in candidates if not has_calendar_conflict(s, events)]
