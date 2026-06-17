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

    # ▼ 変更点：CSVの内容に変更があったかをハッシュ値で検知
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
        
        # ▼ 変更点：「パーセンテージ」ではなく「絶対時間（マイナス150秒）」のバッファを採用
        # 例：10分(600秒)設定なら 450秒経過で許可。70分(4200秒)なら 4050秒経過で許可。
        required_elapsed_seconds = max(0, (interval_minutes * 60) - 150)
        actual_elapsed_seconds = current_time - last_run_time

        if actual_elapsed_seconds < required_elapsed_seconds:
            print(f"⏩ {target_name}: スキップ（設定: {interval_minutes}分おき）")
            continue

        print(f"\n🔍 ターゲット確認中: {target_name}")
        
        if driver is None:
            driver = webdriver.Chrome(options=options)

        target_start_time = time.time() # サイトごとの計測開始

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
                send_discord(f"🚨 【{target_name}】で内容に変化がありました！\n🔗 URL: {target['url']}")
                history_data[target_name] = current_text
            
            history_data[f"{target_name}_last_run"] = current_time

        except Exception as e:
            print(f"❌ {target_name} の確認中にエラー発生。")
        
        # サイトごとの実測処理時間を記録（コスト計算用）
        target_end_time = time.time()
        history_data[f"{target_name}_measured_sec"] = target_end_time - target_start_time

    if driver is not None:
        driver.quit()

    # ▼ 変更点：CSVに変更があった場合、記録された実測値をもとに月間コストを計算して通知
    if csv_changed:
        total_monthly_minutes = 0
        
        for target in targets:
            if not target.get('name'): continue
            name = target['name']
            
            try:
                interval = float(target.get('interval', 10))
            except ValueError:
                interval = 10
            if interval <= 0: interval = 10
            
            # 実測値がない（エラー等）場合は安全マージンを取って30秒で計算
            measured_sec = history_data.get(f"{name}_measured_sec", 30)
            
            # 月間（30日）の実行回数 × 1回あたりの消費分数
            runs_per_month = (30 * 24 * 60) / interval
            target_monthly_min = runs_per_month * (measured_sec / 60)
            total_monthly_minutes += target_monthly_min

        cost_message = (
            f"📊 **監視リスト(CSV)の更新を検知しました**\n"
            f"実測時間に基づく現在のペースでの想定消費コスト:\n"
            f"**約 {int(total_monthly_minutes)} / 2000 分 (月間)**"
        )
        send_discord(cost_message)
        print(cost_message)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_data, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    check_targets()
