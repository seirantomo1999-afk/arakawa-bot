# -*- coding: utf-8 -*-
"""
荒川区テニスコート予約bot - スクレイピングモジュール
要件定義: REQUIREMENTS.md に準拠
"""
from __future__ import annotations

import os
import re
import sys
import time
from contextlib import suppress
from datetime import date, timedelta
from typing import NamedTuple

import jpholiday

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

# ===== 設定 =====
# ローカルで画面を出してデバッグしたいなら True にする
SHOW_BROWSER = True   # GitHub Actions 上では自動で headless になるのでこのままでOK

# デバッグ用: 次へボタンの押下回数上限（None で無制限）
MAX_NEXT_CLICKS = None

# デバッグ用: True だと土日祝以外（平日）も候補にする（埋まってないので突合検証しやすい）
INCLUDE_WEEKDAYS_FOR_DEBUG = True

# デバッグ用: True だと2時間枠・開始時刻制限を外す（全枠で突合検証できる）
INCLUDE_ALL_TIME_SLOTS_FOR_DEBUG = True

# テスト用: True だと最初の候補で予約ボタン押下まで実行する
DO_BOOK_FIRST_CANDIDATE = True

# ===== 要件定義（REQUIREMENTS.md）に基づく定数 =====
# 予約対象のコート（公園名・施設名のキーワード。いずれかに部分一致すれば対象）
PARK_KEYWORDS = (
    "東尾久運動場",
    "区民運動場",
    "自然公園庭球場",
    "宮前公園庭球場",
)

# 予約枠: 2時間固定、開始時刻は以下のみ
VALID_START_TIMES = ("09:00", "10:00", "11:00", "13:00", "15:00", "17:00", "19:00")

# 直近予約禁止: 実行日から何日以内の枠を除外するか
MIN_DAYS_AHEAD = 3


class SlotInfo(NamedTuple):
    """空き枠の構造化情報（カレンダー連携・予約ロジック用）"""
    date_obj: date
    date_text: str
    start_time: str  # "09:00"
    end_time: str    # "11:00"
    time_text: str   # "09:00～11:00"
    court: str
    display_line: str  # 従来形式の1行 "[n] 日付, 時間, コート に空きがあります。"
    button_id: str = ""  # 予約ボタン要素のid（例: "button0_8"）

    def to_calendar_format(self) -> str:
        """カレンダーと共通の形式（ISO日付 + 時刻）で出力。例: 2026-03-02 09:00-11:00"""
        if not self.start_time or not self.end_time:
            return f"{self.date_obj.isoformat()} {self.time_text}"
        return f"{self.date_obj.isoformat()} {self.start_time}-{self.end_time}"


def _parse_reiwa_date(date_text: str) -> date | None:
    """'令和07年11月14日(金)' 形式を date に変換。失敗時は None"""
    m = re.search(r"令和(\d+)年(\d+)月(\d+)日", date_text)
    if not m:
        return None
    ryear, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # 令和1年=2019
    year = 2018 + ryear
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_time_range(time_text: str) -> tuple[str, str] | None:
    """'09:00～11:00' 形式から (開始, 終了) を取得。失敗時は None"""
    time_text = time_text.replace(" ", "").replace("\n", "")
    m = re.search(r"(\d{1,2}:\d{2})[～\-〜−-]+(\d{1,2}:\d{2})", time_text)
    if not m:
        return None
    return (m.group(1), m.group(2))


def _is_weekend_or_holiday(d: date) -> bool:
    """土日祝かどうか"""
    if d.weekday() >= 5:  # 土日
        return True
    return jpholiday.is_holiday(d)


def _is_valid_slot(start_time: str, end_time: str) -> bool:
    """要件: 2時間固定、開始時刻が VALID_START_TIMES のいずれか（デバッグ時は時間が取れればOK）"""
    if not start_time or not end_time:
        return False
    if INCLUDE_ALL_TIME_SLOTS_FOR_DEBUG:
        # HH:MM 形式が取れていれば突合用にOK（カレンダー突合の検証しやすさ優先）
        return bool(re.match(r"\d{1,2}:\d{2}", start_time) and re.match(r"\d{1,2}:\d{2}", end_time))
    if start_time not in VALID_START_TIMES:
        return False
    expected_ends = {
        "09:00": "11:00", "11:00": "13:00", "13:00": "15:00",
        "15:00": "17:00", "17:00": "19:00", "19:00": "21:00",
    }
    return expected_ends.get(start_time) == end_time


def _is_court_in_scope(court_text: str) -> bool:
    """PARK_KEYWORDS に含まれる公園のコートか"""
    for kw in PARK_KEYWORDS:
        if kw in court_text:
            return True
    return False


def _is_within_min_days(slot_date: date, min_days: int) -> bool:
    """実行日から min_days 以内なら True（除外対象）"""
    today = date.today()
    threshold = today + timedelta(days=min_days)
    return slot_date <= threshold


def _filter_slots_by_requirements(slots: list[SlotInfo]) -> list[SlotInfo]:
    """要件に合致する枠のみに絞る"""
    today = date.today()
    result = []
    for s in slots:
        if not INCLUDE_WEEKDAYS_FOR_DEBUG and not _is_weekend_or_holiday(s.date_obj):
            continue
        if _is_within_min_days(s.date_obj, MIN_DAYS_AHEAD):
            continue
        if not _is_valid_slot(s.start_time, s.end_time):
            continue
        if not _is_court_in_scope(s.court):
            continue
        result.append(s)
    return result


# ===== ステルス系（都営のやつとほぼ統一） =====

def add_basic_stealth(driver):
    """webdriver 感を多少薄める（任意・失敗しても無視）"""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            """}
        )
    except Exception:
        pass


def build_options(headless: bool) -> Options:
    """都営スクレイパと同系統の ChromeOptions を構築"""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")

    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    # 必須ではないけど多少ステルス寄り
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        # headless 時は画像 OFF で高速化してもよいが、今回はそこまでしない
    }
    opts.add_experimental_option("prefs", prefs)

    return opts


def make_driver() -> webdriver.Chrome:
    """
    都営スクレイパと同じノリで driver を作る。
    - ローカル: SHOW_BROWSER の値で headless 切り替え
    - GitHub Actions: 強制 headless
    """
    headless = (not SHOW_BROWSER) or (os.environ.get("GITHUB_ACTIONS") == "true")
    options = build_options(headless)

    # ChromeDriverManager でランナー環境に合ったドライバを取得（ここが都営と同じ発想）
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # ステルス軽く
    add_basic_stealth(driver)
    return driver


# ===== ここから Arakawa 専用ロジック =====

def open_and_login(driver):
    wait = WebDriverWait(driver, 20)

    # STEP0: 最初のページ
    url = "https://shisetsu.city.arakawa.tokyo.jp/stagia/reserve/gin_menu"
    driver.get(url)

    # STEP1: 「多機能操作」ボタン押下
    multi_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="contents"]/ul[1]/li[2]/dl/dt/form/input[@type="image"]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", multi_btn)
    multi_btn.click()

    # STEP2: ID/PW 入力
    user_box = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="user"]')))
    pass_box = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="password"]')))

    user_box.clear()
    pass_box.clear()

    # ★ ここは本当は環境変数とかに逃がした方が安全
    user_box.send_keys("90081")
    pass_box.send_keys("1929")

    # STEP3: ログインボタン押下
    login_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="login-area"]/form/p/input')
        )
    )
    login_btn.click()

    # STEP4: お気に入り をクリック
    next_menu = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="local-navigation"]/dd/ul/li[8]/a')
        )
    )

    driver.execute_script("arguments[0].scrollIntoView(true);", next_menu)
    next_menu.click()

    # STEP5: チェックボックス input[11] をクリック
    checkbox = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="contents"]/form[1]/div/div/dl/dd[2]/input[15]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
    checkbox.click()

    # STEP6: 検索ボタンクリック（#btnOK）
    search_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="btnOK"]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_btn)
    search_btn.click()


def navigate_from_menu_to_search(driver):
    """
    ログイン後のメニュー画面から検索結果画面へ遷移（STEP4〜6）。
    予約完了でメニューに戻った後に呼ぶ。
    """
    wait = WebDriverWait(driver, 20)
    next_menu = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="local-navigation"]/dd/ul/li[8]/a')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", next_menu)
    next_menu.click()

    checkbox = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="contents"]/form[1]/div/div/dl/dd[2]/input[3]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
    checkbox.click()

    search_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, '//*[@id="btnOK"]'))
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_btn)
    search_btn.click()


def _build_block_time_mapping(block_elem) -> dict:
    """
    施設ブロック内の時間ヘッダー行から、列インデックス→時間帯のマッピングを構築する。
    block_elem: table または tbody など、施設ブロックのルート要素
    戻り値: {"1": "09:00-09:30", "2": "09:30-10:00", ...}
    """
    mapping = {}
    try:
        # ブロック内の th[id^='td'] をすべて取得
        ths = block_elem.find_elements(
            By.XPATH, ".//th[starts-with(@id,'td') and contains(@id,'_')]"
        )
        for th in ths:
            tid = th.get_attribute("id") or ""
            m = re.match(r"td\d+_(\d+)$", tid)
            if m:
                col_idx = m.group(1)
                raw = th.text.replace("\n", "").replace(" ", "").strip()
                if raw:
                    mapping[col_idx] = raw
    except Exception:
        pass
    return mapping


def scrape_one_day(driver) -> list[SlotInfo]:
    """検索結果画面で class='ok' の td を列挙し、SlotInfo のリストを返す。"""
    wait = WebDriverWait(driver, 20)
    results: list[SlotInfo] = []

    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])

    ok_cells = driver.find_elements(By.XPATH, "//td[contains(@class,'ok')]")

    try:
        date_h3 = driver.find_element(By.XPATH, "//*[@id='contents']//h3")
        date_text = " ".join(date_h3.text.split())
    except Exception as e:
        print("日付取得エラー:", e)
        date_text = "日付不明"

    date_obj = _parse_reiwa_date(date_text)

    print("=== 空き枠一覧（日付＋時間＋コート） ===")

    for i, td in enumerate(ok_cells, start=1):
        td_id = td.get_attribute("id") or ""
        col_index = "?"
        time_text = "時間不明"
        court_text = "コート不明"

        m = re.match(r"td\d+_(\d+)$", td_id)
        if m:
            col_index = m.group(1)

            try:
                row_tr = td.find_element(By.XPATH, "./ancestor::tr[1]")
            except Exception:
                row_tr = None

            header_elem = None
            if row_tr is not None:
                try:
                    tbody = row_tr.find_element(By.XPATH, "./ancestor::tbody[1]")
                    header_elem = tbody.find_element(By.XPATH, "./preceding-sibling::thead[1]")
                except Exception:
                    try:
                        table = row_tr.find_element(By.XPATH, "./ancestor::table[1]")
                        header_elem = table.find_element(By.XPATH, ".//thead[1]")
                    except Exception:
                        pass

            if row_tr is not None:
                try:
                    court_th = row_tr.find_element(By.XPATH, "./th[1]")
                    court_text = " ".join(court_th.text.split())
                except Exception:
                    pass

                if header_elem is not None:
                    time_mapping = _build_block_time_mapping(header_elem)
                    time_text = time_mapping.get(col_index, "時間不明")

        start_time, end_time = "", ""
        if parsed := _parse_time_range(time_text):
            start_time, end_time = parsed

        display_line = f"[{i}] {date_text}, {time_text}, {court_text} に空きがあります。"

        # td 内の input[type=image] から実際の button id を取得（td_id と別体系のため）
        button_id = ""
        try:
            btn = td.find_element(By.XPATH, ".//input[@type='image']")
            button_id = (btn.get_attribute("id") or "").strip()
        except Exception:
            pass

        slot = SlotInfo(
            date_obj=date_obj or date.today(),
            date_text=date_text,
            start_time=start_time,
            end_time=end_time,
            time_text=time_text,
            court=court_text,
            display_line=display_line,
            button_id=button_id,
        )
        results.append(slot)

    return results


def init_calendar_cache():
    """
    カレンダーAPIを呼び出し、予定をキャッシュする。
    ブラウザ起動前に呼ぶことで、失敗時は即座に分かる。
    戻り値: (events_cache or None, filter_by_calendar_func or None)
    """
    try:
        from arakawa_calendar import (
            CALENDAR_SCOPES,
            TOKEN_PATH,
            fetch_calendar_busy_ranges,
            filter_by_calendar,
            get_google_creds,
        )

        creds = get_google_creds(CALENDAR_SCOPES)
        events_cache = fetch_calendar_busy_ranges(creds)
        return events_cache, filter_by_calendar
    except Exception as e:
        import traceback

        print(">>> カレンダーAPI: エラー（以下はカレンダー突合なしで続行）", flush=True)
        traceback.print_exc()
        return None, None


def try_book_first_candidate(driver, first_slot: SlotInfo) -> bool:
    """
    最初の候補で予約を完了する。
    ① slot の button_id（例: button0_8）をクリック
    ② btnYyList をクリック
    ③ ryosyuhhSelect で「振込納付支払い」(value=7) を選択

    ⑤ contents/p/a をクリック
    ⑥ 確認ダイアログで OK
    ⑦ 最終 contents/p/a でメニューへ戻る
    成功で True、失敗で False
    """
    if not first_slot.button_id:
        return False
    try:
        slot_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, first_slot.button_id))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", slot_btn)
        time.sleep(0.5)  # スクロール反映待ち
        slot_btn.click()
        time.sleep(1)  # 画面遷移待ち

        yy_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "btnYyList"))
        )
        yy_btn.click()
        time.sleep(1)

        select_elem = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ryosyuhhSelect"))
        )
        Select(select_elem).select_by_value("7")  # 振込納付支払い
        time.sleep(0.5)

        ok_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='btnOK']/a"))
        )
        ok_btn.click()
        time.sleep(1)

        # ④の後、URL切り替え先で「他の利用者が既に予約済です」がないか確認
        try:
            body_elem = driver.find_element(By.TAG_NAME, "body")
            if "他の利用者が既に予約済です" in (body_elem.text or ""):
                # 時間差で先を越された → 戻るボタンでメニューへ戻り、STEP4から再開
                back_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//*[@id='contents']/p/input"))
                )
                back_btn.click()
                time.sleep(1)
                navigate_from_menu_to_search(driver)
                return False
        except Exception:
            pass

        contents_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[@id='contents']/p/a"))
        )
        contents_link.click()
        time.sleep(0.5)

        # 確認ダイアログ（予約を確定してもよろしいですか?）で OK
        try:
            alert = WebDriverWait(driver, 5).until(EC.alert_is_present())
            alert.accept()
        except Exception:
            pass
        time.sleep(0.5)

        # ⑦ 最後の contents/p/a で予約完了→メニューに戻る
        try:
            final_link = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//*[@id='contents']/p/a"))
            )
            final_link.click()
        except Exception:
            pass

        return True
    except Exception:
        return False


def scrape_all_days(driver, events_cache=None, filter_by_calendar_func=None) -> tuple[list[SlotInfo], list[SlotInfo]]:
    """
    全日の検索結果をスクレイピングする。
    各ページごとに空き枠表示・カレンダー突合を行い、その後「次へ」を押す。
    events_cache, filter_by_calendar_func は init_calendar_cache() の戻り値を渡す。
    戻り値: (全スロット一覧, 予約完了したスロット一覧)
    """
    NEXT_XPATH = '//*[@id="contents"]/div[2]/div/ul/li[2]/a[1]'
    all_results: list[SlotInfo] = []
    booked_slots: list[SlotInfo] = []
    next_clicks = 0

    while True:
        print("\n=== 新しい日付のスクレイピング開始 ===")
        day_results = scrape_one_day(driver)
        all_results.extend(day_results)

        # このページの空き枠を即座に表示（次へを押す前）
        if day_results:
            print("\n--- この日の空き枠 ---")
            for slot in day_results:
                print(slot.display_line)

            # 要件フィルタ → カレンダー突合
            candidates = get_reservation_candidates(day_results)
            if candidates:
                if events_cache is not None and filter_by_calendar_func is not None:
                    calendar_ok = filter_by_calendar_func(candidates, events_cache=events_cache)
                    excluded = len(candidates) - len(calendar_ok)
                    print(f"\n  [カレンダー突合] 候補{len(candidates)}件→競合除外{excluded}件→残り{len(calendar_ok)}件", flush=True)
                else:
                    calendar_ok = candidates  # カレンダー未使用
                    print("\n  [カレンダー未使用: events_cacheなし]", flush=True)
                if calendar_ok:
                    print("\n--- カレンダー競合なしの予約候補（この日） ---")
                    for s in calendar_ok:
                        print(f"  {s.to_calendar_format()}  {s.court}")
                    if DO_BOOK_FIRST_CANDIDATE:
                        first_slot = calendar_ok[0]
                        if try_book_first_candidate(driver, first_slot):
                            booked_slots.append(first_slot)
                            navigate_from_menu_to_search(driver)
                            time.sleep(1)
                            continue  # 検索結果ページ1からループ再開
            else:
                print(f"\n  [カレンダー突合] 候補0件（土日祝・3日以降・2時間枠・対象公園のいずれかで除外）→突合スキップ", flush=True)

        if MAX_NEXT_CLICKS is not None and next_clicks >= MAX_NEXT_CLICKS:
            print(f"\n→ 次へボタン {MAX_NEXT_CLICKS} 回で終了")
            break

        try:
            next_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, NEXT_XPATH))
            )
        except Exception:
            print("\n→ 次へが見つからず終了")
            break

        print("\n→ 次へをクリック")
        try:
            next_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", next_btn)
        next_clicks += 1

        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//*[@id="contents"]')
                )
            )
        except Exception:
            print("→ 次の結果ロード失敗…")
            break

    return all_results, booked_slots


def get_reservation_candidates(slots: list[SlotInfo]) -> list[SlotInfo]:
    """
    要件定義に合致する予約候補のみを返す。
    （土日祝・3日以降・2時間枠・開始時刻09/11/13/15/17/19時・PARK_KEYWORDS対象）
    """
    return _filter_slots_by_requirements(slots)


if __name__ == "__main__":
    # ブラウザ起動前にカレンダーAPIを呼ぶ（失敗時は即座に分かる）
    print("=== カレンダーAPI初期化（ブラウザ起動前） ===", flush=True)
    events_cache, filter_by_calendar_func = init_calendar_cache()
    print("", flush=True)

    driver = make_driver()
    try:
        open_and_login(driver)
        all_results, booked_slots = scrape_all_days(
            driver, events_cache=events_cache, filter_by_calendar_func=filter_by_calendar_func
        )
        print("\n=== 全ページ処理完了 ===")
        if booked_slots:
            print("\n--- 予約完了したコート ---")
            for s in booked_slots:
                # arakawa_gmail が stdout から解析するためのプレフィックス
                print(f"BOOKED: {s.to_calendar_format()}  {s.court}")
        if sys.stdin.isatty():
            input("\nEnterキーを押すとブラウザを閉じます...")
    finally:
        with suppress(Exception):
            driver.quit()
