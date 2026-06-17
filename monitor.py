import csv
import io
import time
import requests
import os
import json
import hashlib
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# 本番環境ではGitHub Secretsから自動的に読み込まれます
# ==========================================
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
CSV_URL = os.environ.get("CSV_URL", "")
# ==========================================

def send_discord(message):
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print("🔔 [通知シミュレーション] Discord設定がないためコンソール出力のみ:")
        print(f"   -> {message}")
        return

    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        requests.post(url, headers=headers, json={"content": message})
    except Exception as e:
        print(f"❌ 通知の送信に失敗しました: {e}")

def check_targets():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 本番監視プロセスを開始します...")
    
    # スプレッドシート（CSV）の読み込み
    try:
        res = requests.get(CSV_URL)
        res.encoding = 'utf-8'
        csv_text = res.text
        targets = list(csv.DictReader(io.StringIO(csv_text)))
    except Exception as e:
        print(f"❌ スプレッドシートの読み込みに失敗しました: {e}")
        return

    HISTORY_FILE = "history.json"
    history_data = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history_data = json.load(f)
        except json.JSONDecodeError:
            history_data = {}

    current_csv_hash = hashlib.md5(csv_text.encode('utf-8')).hexdigest()
    csv_changed = False
    if current_csv_hash != history_data.get("csv_hash"):
        csv_changed = True
        history_data["csv_hash"] = current_csv_hash
        print("🔄 スプレッドシートの変更を検知しました。コスト再計算を予約します。")

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--log-level=3')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])

    driver = None

    for target in targets:
        if not target.get('name') or not target.get('url'):
            continue

        target_name = target['name']
        try:
            interval_minutes = float(target.get('interval', 10))
        except ValueError:
            interval_minutes = 10

        last_run_time = history_data.get(f"{target_name}_last_run", 0)
        current_time = time.time()
        
        required_elapsed_seconds = max(0, (interval_minutes * 60) - 150)
        actual_elapsed_seconds = current_time - last_run_time

        if actual_elapsed_seconds < required_elapsed_seconds:
            print(f"⏩ {target_name}: スキップ（設定: {interval_minutes}分おき）")
            continue

        print(f"\n🔍 ターゲット確認中: {target_name}")
        
        if driver is None:
            driver = webdriver.Chrome(options=options)

        target_start_time = time.time() # 計測開始

        try:
            driver.get(target['url'])
            elements = WebDriverWait(driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, target['wait_element']))
            )
            time.sleep(2) 

            current_text = "\n".join([elem.text.strip() for elem in elements])
            previous_text = history_data.get(target_name, "")

            if previous_text == "":
                history_data[target_name] = current_text
            elif current_text != previous_text:
                send_discord(f" 【{target_name}】で内容に変化がありました\n URL: {target['url']}")
                history_data[target_name] = current_text
            
            history_data[f"{target_name}_last_run"] = current_time

        except Exception as e:
            print(f"❌ {target_name} の確認中にエラー発生。")
        
        # 実測時間を記録
        target_end_time = time.time()
        history_data[f"{target_name}_measured_sec"] = target_end_time - target_start_time

    if driver is not None:
        driver.quit()

    # ==========================================
    # ▼ コスト計算と内訳生成のロジック（アップデート部分）
    # ==========================================
    if csv_changed:
        total_monthly_minutes = 0
        breakdown_text = ""
        
        # ① 基本システムの起動コストを計算
        # GitHubのサーバー準備やpip installには毎回時間がかかるため、約25秒として推定
        # ※一番短いintervalに合わせて全体が起動すると仮定して計算
        min_interval = 10
        for target in targets:
            try:
                iv = float(target.get('interval', 10))
                if 0 < iv < min_interval:
                    min_interval = iv
            except ValueError:
                pass
                
        runs_per_month_base = (30 * 24 * 60) / min_interval
        base_overhead_min = runs_per_month_base * (25 / 60) # 1回あたり25秒消費
        
        total_monthly_minutes += base_overhead_min
        breakdown_text += f"・⚙️ **基本コード実行**: 約 {int(base_overhead_min)} 分 (サーバー準備等)\n"
        
        # ② 各ターゲットの実測に基づくコストを計算
        for target in targets:
            if not target.get('name'): continue
            name = target['name']
            
            try:
                interval = float(target.get('interval', 10))
            except ValueError:
                interval = 10
            if interval <= 0: interval = 10
            
            # 実測値がない場合は安全マージンを取って30秒で仮計算
            measured_sec = history_data.get(f"{name}_measured_sec", 30)
            
            runs_per_month = (30 * 24 * 60) / interval
            target_monthly_min = runs_per_month * (measured_sec / 60)
            
            total_monthly_minutes += target_monthly_min
            breakdown_text += f"・🔍 **{name}**: 約 {int(target_monthly_min)} 分 (間隔:{interval}分 / 実測:{measured_sec:.1f}秒)\n"

        cost_message = (
            f" **監視リスト(CSV)の更新を検知しました**\n"
            f"実測時間に基づく現在のペースでの想定消費コスト:\n"
            f"**合計: 約 {int(total_monthly_minutes)} / 2000 分 (月間)**\n\n"
            f"【月間コスト内訳】\n{breakdown_text}"
        )
        send_discord(cost_message)
        print(cost_message)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    check_targets()
