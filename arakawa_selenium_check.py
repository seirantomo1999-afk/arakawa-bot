# -*- coding: utf-8 -*-

import os
import re
import time
from contextlib import suppress

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

# ===== 設定 =====
# ローカルで画面を出してデバッグしたいなら True にする
SHOW_BROWSER = True   # GitHub Actions 上では自動で headless になるのでこのままでOK


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

    # STEP4: 次のメニュー（li[8]/a）をクリック
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


def scrape_one_day(driver):
    """検索結果画面で class='ok' の td を列挙しつつ、『日付』『時間』『コート名』を表示。"""
    wait = WebDriverWait(driver, 20)

    results = []

    print("=== list_ok_cells START ===")
    print("current_url(before):", driver.current_url)
    print("window_handles(before):", driver.window_handles)

    # 念のため、ウィンドウが増えていたら最後のものに切り替え
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])
        print("→ 新しいウィンドウに切り替えました")
        print("current_url(after):", driver.current_url)

    # ok セルを取得
    ok_cells = driver.find_elements(By.XPATH, "//td[contains(@class,'ok')]")

    # 日付テキスト取得（例: "令和07年11月14日(金)"）
    try:
        date_h3 = driver.find_element(By.XPATH, "//*[@id='contents']//h3")
        date_text = " ".join(date_h3.text.split())
    except Exception as e:
        print("日付取得エラー:", e)
        date_text = "日付不明"

    print("DEBUG: date_text =", date_text)

    print("=== 空き枠一覧（日付＋時間＋コート） ===")
    print("件数:", len(ok_cells))

    for i, td in enumerate(ok_cells, start=1):
        td_id = td.get_attribute("id") or ""

        col_index = "?"
        time_text = "時間不明"
        court_text = "コート不明"

        # 1) td11_7 / td12_3 みたいなIDから「末尾の列番号」を取り出す
        m = re.match(r"td\d+_(\d+)$", td_id)
        if m:
            col_index = m.group(1)  # "7" とか

            # 2) 自分の行を取得
            try:
                row_tr = td.find_element(By.XPATH, "./ancestor::tr[1]")
            except Exception:
                row_tr = None

            # 3) 時間ヘッダーを含む要素を取得
            # 構造: table > thead(時間) + tbody(コート行)... の繰り返し。thead と tbody は兄弟。
            # tbody に属する行の場合、直前の thead がそのブロックの時間ヘッダー
            header_elem = None
            if row_tr is not None:
                try:
                    tbody = row_tr.find_element(By.XPATH, "./ancestor::tbody[1]")
                    # tbody の直前の thead がこの施設ブロックの時間ヘッダー
                    header_elem = tbody.find_element(By.XPATH, "./preceding-sibling::thead[1]")
                except Exception:
                    try:
                        # tbody がない場合（例: 暗黙の tbody）は table 内の thead を探す
                        table = row_tr.find_element(By.XPATH, "./ancestor::table[1]")
                        header_elem = table.find_element(By.XPATH, ".//thead[1]")
                    except Exception:
                        pass

            # 4) コート名：同じ行の先頭 th
            if row_tr is not None:
                try:
                    court_th = row_tr.find_element(By.XPATH, "./th[1]")
                    court_text = " ".join(court_th.text.split())
                except Exception:
                    pass

                # 5) 時間：同じ施設ブロックの thead から列→時間マッピングを取得
                if header_elem is not None:
                    time_mapping = _build_block_time_mapping(header_elem)
                    time_text = time_mapping.get(col_index, "時間不明")

        line = f"[{i}] {date_text}, {time_text}, {court_text} に空きがあります。"
        results.append(line)

    return results


def scrape_all_days(driver):
    NEXT_XPATH = '//*[@id="contents"]/div[2]/div/ul/li[2]/a[1]'

    all_results = []

    while True:
        print("=== 新しい日付のスクレイピング開始 ===")

        day_results = scrape_one_day(driver)
        all_results.extend(day_results)

        # 「次へ」ボタンを探す
        try:
            next_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, NEXT_XPATH))
            )
        except Exception:
            print("→ 次へが見つからず終了")
            break

        print("→ 次へをクリック")
        next_btn.click()

        # 次の日付の結果がロードされるまで待機
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//*[@id="contents"]')
                )
            )
        except Exception:
            print("→ 次の結果ロード失敗…")
            break

    return all_results


if __name__ == "__main__":
    driver = make_driver()
    try:
        open_and_login(driver)
        results = scrape_all_days(driver)

        print("=== 最終結果 ===")
        for line in results:
            print(line)

        # ここで results をファイルに書いたり、別のモジュールから呼び出して
        # Gmail で送ったりすればOK
    finally:
        with suppress(Exception):
            driver.quit()
