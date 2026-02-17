# -*- coding: utf-8 -*-
"""
Google Calendar API 検証用スクリプト
予定を取得して件数と一覧を表示し、突合ロジックをテストする。
実行: python scripts/verify_calendar.py
"""
import sys

# プロジェクト直下をパスに追加
sys.path.insert(0, ".")


def main():
    from arakawa_calendar import (
        CALENDAR_SCOPES,
        get_google_creds,
        fetch_calendar_busy_ranges,
        has_calendar_conflict,
    )
    from arakawa_selenium_check import SlotInfo

    print("=== Google Calendar 予定取得の検証 ===\n")
    creds = get_google_creds(CALENDAR_SCOPES)
    print("認証OK\n")

    busy_ranges = fetch_calendar_busy_ranges(creds)
    print(f"取得件数: {len(busy_ranges)} 件\n")
    if busy_ranges:
        print("--- 予定一覧 ---")
        for i, (start_dt, end_dt) in enumerate(busy_ranges, 1):
            print(f"  [{i}] {start_dt} 〜 {end_dt}")
        print()
    else:
        print("(予定はありません)\n")

    # 突合ロジックのテスト: 各予定の日付・時刻に合わせた枠を作成し、競合判定を実行
    if busy_ranges:
        print("--- 突合テスト（各予定と同日同時間帯の枠で has_calendar_conflict を実行） ---")
        for i, (ev_start, ev_end) in enumerate(busy_ranges[:5], 1):
            # 予定の日付・開始時刻を取得（JST 想定）
            from datetime import timezone, timedelta
            jst = timezone(timedelta(hours=9))
            ev_start_jst = ev_start.astimezone(jst) if ev_start.tzinfo else ev_start
            slot_date = ev_start_jst.date()
            start_str = ev_start_jst.strftime("%H:%M")
            end_str = ev_end.astimezone(jst).strftime("%H:%M") if ev_end.tzinfo else ev_end.strftime("%H:%M")
            slot = SlotInfo(
                date_obj=slot_date,
                date_text="",
                start_time=start_str,
                end_time=end_str,
                time_text=f"{start_str}～{end_str}",
                court="テストコート",
                display_line="",
            )
            conflict = has_calendar_conflict(slot, busy_ranges)
            print(f"  枠 {slot_date} {start_str}-{end_str} → 競合={'あり' if conflict else 'なし'}")
    print("\n=== 検証完了 ===")


if __name__ == "__main__":
    main()
