from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import re 


def create_driver(show_browser: bool = False) -> webdriver.Chrome:
    options = Options()
    if not show_browser:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    return driver


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

    user_box.send_keys("90081")
    pass_box.send_keys("1929")

    # STEP3: ログインボタン押下
    login_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="login-area"]/form/p/input')
        )
    )
    login_btn.click()

    print("ログインボタン押下完了！")

   # STEP4: 次のメニュー（li[8]/a）をクリック
    next_menu = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="local-navigation"]/dd/ul/li[8]/a')
        )
    )

    driver.execute_script("arguments[0].scrollIntoView(true);", next_menu)
    next_menu.click()

    print("メニュー li[8] をクリック完了！")

    # STEP4: チェックボックス input[11] をクリック
    checkbox = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="contents"]/form[1]/div/div/dl/dd[2]/input[11]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
    checkbox.click()
    print("→ チェックつけた！")

    # STEP5: 検索ボタンクリック（#btnOK）
    search_btn = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, '//*[@id="btnOK"]')
        )
    )
    driver.execute_script("arguments[0].scrollIntoView(true);", search_btn)
    search_btn.click()

    print("→ 検索ボタンを押しました！")
    print("→ 次のページに遷移しているはずです")

def scrape_one_day(driver):
    """検索結果画面で class='ok' の td を列挙しつつ、
    『日付』『時間』『コート名』を表示する。
    """
    wait = WebDriverWait(driver, 20)

    # ★ この1日分の結果を入れるリスト
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

    # ★ 日付テキスト取得（例: "令和07年11月14日(金)"）
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
        td_class = td.get_attribute("class") or ""

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

            # 3) コート名：同じ行の先頭 th
            if row_tr is not None:
                try:
                    court_th = row_tr.find_element(By.XPATH, "./th[1]")
                    court_text = " ".join(court_th.text.split())
                except Exception:
                    pass

                # 4) 時間：自分の行より上にある時間行から同じ列番号の th を探す
                try:
                    header_tr = row_tr.find_element(
                        By.XPATH,
                        (
                            "./preceding-sibling::tr"
                            f"[.//th[starts-with(@id,'td') "
                            f"and substring-after(@id,'_')='{col_index}']][1]"
                        )
                    )
                    time_th = header_tr.find_element(
                        By.XPATH,
                        (
                            f".//th[starts-with(@id,'td') "
                            f"and substring-after(@id,'_')='{col_index}'][1]"
                        )
                    )
                    raw = time_th.text
                    time_text = raw.replace("\n", "").replace(" ", "") or "時間不明"
                except Exception:
                    # 見つからなければページ全体から同じ列番号を探す
                    try:
                        fallback_th = driver.find_element(
                            By.XPATH,
                            (
                                f"//th[starts-with(@id,'td') "
                                f"and substring-after(@id,'_')='{col_index}'][1]"
                            )
                        )
                        raw = fallback_th.text
                        time_text = raw.replace("\n", "").replace(" ", "") or "時間不明"
                    except Exception:
                        time_text = "時間不明"

        # 日本語のきれいな1行メッセージを作成
        line = f"[{i}] {date_text}, {time_text}, {court_text} に空きがあります。"

        print(line)          # コンソール表示
        results.append(line) # ★ この日のリストに追加

    # ★ この日分の結果を返す
    return results

def scrape_all_days(driver):
    NEXT_XPATH = '//*[@id="contents"]/div[2]/div/ul/li[2]/a[1]'

    # ★ 全日分の結果を入れるリスト
    all_results = []

    while True:
        print("=== 新しい日付のスクレイピング開始 ===")

        # 1日分の結果を取得
        day_results = scrape_one_day(driver)

        # まとめ用リストに追加
        all_results.extend(day_results)

        # 「次へ」ボタンを探す
        try:
            next_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, NEXT_XPATH))
            )
        except:
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
        except:
            print("→ 次の結果ロード失敗…")
            break

    # ★ 全日分を返す
    return all_results


# ⑤ main ← 最後に書く
if __name__ == "__main__":
    driver = create_driver(show_browser=True)
    try:
        open_and_login(driver)
        scrape_all_days(driver)  # ← ここが list_ok_cells から変更点！
    finally:
        driver.quit()
