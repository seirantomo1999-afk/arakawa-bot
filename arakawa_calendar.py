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

from arakawa_config import JST

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
            if os.getenv("GITHUB_ACTIONS") == "true":
                raise RuntimeError("Google認証を更新できません。ローカルでtoken.jsonを再発行してください。")
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


def _event_range(event) -> tuple[datetime, datetime]:
    """Normalize one event. A reversed or zero-length event still blocks that time."""
    start, end = event.get("start") or {}, event.get("end") or {}
    all_day = "dateTime" not in start
    if not all_day:
        start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
        if start_dt.tzinfo is None or end_dt.tzinfo is None:
            raise ValueError("Calendar event is missing a timezone")
    else:
        start_dt = datetime.combine(date.fromisoformat(start["date"]), time.min, tzinfo=JST)
        end_dt = datetime.combine(date.fromisoformat(end["date"]), time.min, tzinfo=JST)
    repaired = False
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt
        repaired = True
    if end_dt == start_dt:
        end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(minutes=1))
        repaired = True
    if repaired:
        label = " ".join((event.get("summary") or "無題").split())
        print(f"カレンダー予定の範囲を補正して予定ありにしました: {label}", flush=True)
    return start_dt, end_dt


def _fetch_busy_ranges(service, time_min: datetime, time_max: datetime):
    busy = []
    page_token = None
    while True:
        response = service.events().list(
            calendarId="primary", timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
            singleEvents=True, orderBy="startTime", pageToken=page_token,
        ).execute()
        for event in response.get("items", []):
            if event.get("status") != "cancelled":
                busy.append(_event_range(event))
        page_token = response.get("nextPageToken")
        if not page_token:
            return busy


def fetch_calendar_busy_ranges(creds: Credentials) -> list[tuple[datetime, datetime]]:
    """Fetch all pages covering the next 400 days once per scan."""
    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(timezone.utc)
    return _fetch_busy_ranges(service, now, now + timedelta(days=400))


def fetch_events_in_range(service, start_date: date, end_date: date):
    return _fetch_busy_ranges(
        service, datetime.combine(start_date, time.min, tzinfo=JST),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=JST),
    )


def _parse_time_to_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid time")
    return hour * 60 + minute


def has_calendar_conflict(slot: "SlotInfo", events: list[tuple[datetime, datetime]], tz_offset_hours: int = 9) -> bool:
    """
    予約枠の前後2時間に予定が1件でもあれば True（予約不可）。
    例: 13:00-15:00 の枠 → 11:00-17:00 に予定があれば True
    """
    try:
        start_mins = _parse_time_to_minutes(slot.start_time)
        end_mins = _parse_time_to_minutes(slot.end_time)
        if end_mins <= start_mins:
            return True
    except (ValueError, AttributeError):
        return True  # Unknown times cannot be approved for automatic booking.

    tz = timezone(timedelta(hours=tz_offset_hours))
    midnight = datetime.combine(slot.date_obj, time.min, tzinfo=tz)
    check_start_utc = (midnight + timedelta(minutes=start_mins, hours=-HOURS_BUFFER)).astimezone(timezone.utc)
    check_end_utc = (midnight + timedelta(minutes=end_mins, hours=HOURS_BUFFER)).astimezone(timezone.utc)

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

