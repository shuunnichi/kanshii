import csv
import io
import re
import time
import requests
import os
import json
import hashlib
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

try:
    import jpholiday  # 祝日判定用（pip install jpholiday）。無くても動く
except ImportError:
    jpholiday = None

# ==========================================
# 本番環境ではGitHub Secretsから自動的に読み込まれます
# ==========================================
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
CSV_URL = os.environ.get("CSV_URL", "")
# ==========================================

JST = ZoneInfo("Asia/Tokyo")  # GitHub ActionsはUTCで動くので日本時間で判定する
API = "https://discord.com/api/v10"
HISTORY_FILE = "history.json"


# ==========================================
# ▼ Discord送受信
# ==========================================
def discord_post(content, file_paths=None, reply_to=None):
    """メッセージ送信。file_pathsで添付、reply_toでそのメッセージへの返信にする"""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print(f"[通知シミュレーション] {content} / 添付: {file_paths}")
        return

    url = f"{API}/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    payload = {"content": content[:1990]}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}

    opened = []
    try:
        if file_paths:
            files = {}
            for i, path in enumerate(file_paths):
                f = open(path, "rb")
                opened.append(f)
                files[f"files[{i}]"] = (os.path.basename(path), f)
            requests.post(url, headers=headers,
                          data={"payload_json": json.dumps(payload)}, files=files)
        else:
            requests.post(url, headers=headers, json=payload)
    except Exception as e:
        print(f"[エラー] Discord送信に失敗しました: {e}")
    finally:
        for f in opened:
            f.close()


def send_discord(message):
    discord_post(message)


def fetch_new_commands(history_data):
    """前回以降にチャンネルへ投稿されたメッセージを取得（古い順）"""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        return []

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    last_id = history_data.get("discord_last_msg_id")
    params = {"limit": 50}
    if last_id:
        params["after"] = last_id

    try:
        res = requests.get(f"{API}/channels/{DISCORD_CHANNEL_ID}/messages",
                           headers=headers, params=params)
        res.raise_for_status()
        messages = sorted(res.json(), key=lambda m: int(m["id"]))
    except Exception as e:
        print(f"[エラー] Discordのメッセージ取得に失敗しました: {e}")
        return []

    if messages:
        history_data["discord_last_msg_id"] = messages[-1]["id"]

    # 初回は直近30分以内のメッセージだけ処理する（古いコマンドを大量に実行しないため）
    if not last_id:
        limit_ms = (time.time() - 30 * 60) * 1000
        messages = [m for m in messages
                    if (int(m["id"]) >> 22) + 1420070400000 >= limit_ms]

    commands = []
    for m in messages:
        if m.get("author", {}).get("bot"):
            continue
        text = (m.get("content") or "").strip()
        if not text:
            print("[警告] 本文が空のメッセージがあります。Developer Portalで"
                  "MESSAGE CONTENT INTENTが有効か確認してください。")
            continue
        text = text.replace("！", "!").replace("　", " ")
        if text.startswith("!"):
            commands.append((m["id"], text[1:].strip()))
    return commands


# ==========================================
# ▼ スケジュール判定（schedule列）
# ==========================================
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")
DATE_RANGE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:-(\d{1,2})/(\d{1,2}))?$")


def is_holiday(d):
    return bool(jpholiday and jpholiday.is_holiday(d))


def match_condition(cond, now):
    """条件1つに今が当てはまるか。解釈できない書き方ならNoneを返す"""
    cond = cond.lower()
    today = DAYS[now.weekday()]
    if cond == "weekday":
        return now.weekday() < 5 and not is_holiday(now.date())
    if cond == "weekend":
        return now.weekday() >= 5
    if cond == "holiday":
        return is_holiday(now.date())

    # 曜日指定: mon+wed+fri / mon-fri
    if all(p in DAYS for p in cond.split("+")):
        return today in cond.split("+")
    m = re.match(r"^([a-z]{3})-([a-z]{3})$", cond)
    if m and m[1] in DAYS and m[2] in DAYS:
        a, b, t = DAYS.index(m[1]), DAYS.index(m[2]), now.weekday()
        return a <= t <= b if a <= b else (t >= a or t <= b)

    m = TIME_RANGE.match(cond)
    if m:
        start = int(m[1]) * 60 + int(m[2])
        end = int(m[3]) * 60 + int(m[4])
        cur = now.hour * 60 + now.minute
        return start <= cur < end if start <= end else (cur >= start or cur < end)

    m = DATE_RANGE.match(cond)
    if m:
        start = (int(m[1]), int(m[2]))
        end = (int(m[3]), int(m[4])) if m[3] else start
        cur = (now.month, now.day)
        return start <= cur <= end if start <= end else (cur >= start or cur <= end)

    return None


def is_active(schedule, now):
    """
    schedule列の書き方（スペース区切りで複数指定、全部満たすときだけ動く）
      (blank)          … always on
      off              … paused
      weekday          … 平日のみ（祝日除く） / weekend … 土日のみ / holiday … 祝日のみ
      mon-fri, mon+wed+fri … その曜日のみ
      07:00-10:00      … その時間帯のみ
      12/1-12/31, 12/25 … その期間のみ
      off:<条件>        … その条件のときは止める（off:weekend, off:holiday, off:12/24-1/7, off:00:00-06:00）
    """
    schedule = (schedule or "").strip()
    if not schedule:
        return True

    for tok in re.split(r"[;,\s]+", schedule):
        if not tok:
            continue
        if tok.lower() == "off":
            return False
        is_off = tok.lower().startswith("off:")
        cond = tok[4:] if is_off else tok
        result = match_condition(cond, now)
        if result is None:
            print(f"[警告] schedule「{tok}」は解釈できないため無視します")
            continue
        if is_off and result:
            return False
        if not is_off and not result:
            return False
    return True


# ==========================================
# ▼ ページ変化監視（type空欄 / monitor）
# ==========================================
def check_monitor(driver, target, history_data, screenshot=False):
    """戻り値: "first"（初回）/ "changed" / "same" """
    name = target["name"]
    driver.get(target["url"])
    elements = WebDriverWait(driver, 20).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, target["wait_element"]))
    )
    time.sleep(2)
    if screenshot:
        driver.save_screenshot("check.png")

    current_text = "\n".join([elem.text.strip() for elem in elements])
    previous_text = history_data.get(name, "")
    history_data[name] = current_text

    if previous_text == "":
        return "first"
    return "changed" if current_text != previous_text else "same"


# ==========================================
# ▼ 京阪バス延着証明（type: bus_delay）
# ==========================================
def debug_dump(driver, name, reason, reply_to=None):
    """失敗時にスクショとHTMLをDiscordへ送る（修正用）"""
    try:
        driver.save_screenshot("debug.png")
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        discord_post(f"[要確認] {name}: {reason}\n（このHTMLを貼れば修正できます）",
                     ["debug.png", "debug.html"], reply_to)
    except Exception as e:
        print(f"[エラー] デバッグ情報の送信に失敗: {e}")


def search_bus_delay(driver, target, day, h, m, reply_to=None):
    """
    延着証明を検索する。戻り値: ("found" | "none" | "error", サイトのメッセージ)
    サイトの仕組み: 停留所名(name1/name2)と日時(day/hour/min)を入れて「検索・発行」を押すと、
    延着があれば print.aspx（証明書）に移動し、なければ #err-msg1 にメッセージが出る。
    結果画面は result.png に保存する。
    """
    name = target["name"]
    driver.get(target["url"])
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.ID, "search")))

    # 停留所名は自動補完に頼らず値を直接入れる（サイトの表記と完全一致させる）
    driver.execute_script(
        "document.getElementById('name1').value = arguments[0];"
        "document.getElementById('name2').value = arguments[1];",
        target["from_stop"].strip(), target["to_stop"].strip())

    try:
        Select(driver.find_element(By.ID, "day")).select_by_value(day.strftime("%Y/%m/%d"))
    except Exception:
        debug_dump(driver, name, f"日付 {day} が選択肢にありません（7日以内のみ）", reply_to)
        return "error", ""
    Select(driver.find_element(By.ID, "hour")).select_by_value(f"{h:02d}")
    Select(driver.find_element(By.ID, "min")).select_by_value(f"{m // 10 * 10:02d}")

    driver.find_element(By.ID, "search").click()

    def finished(d):
        if "print.aspx" in d.current_url:
            return "found"
        try:
            msg = d.find_element(By.ID, "err-msg1").text.strip()
        except Exception:
            msg = ""
        return msg or False

    try:
        result = WebDriverWait(driver, 20).until(finished)
    except Exception:
        debug_dump(driver, name, "検索結果が20秒以内に表示されませんでした", reply_to)
        return "error", ""

    time.sleep(1)
    driver.save_screenshot("result.png")
    if result == "found":
        return "found", ""
    return "none", result


def auto_bus_check(get_driver, target, history_data):
    """乗車時刻の40分後〜2時間後の間に検索し、見つかったら1日1回だけ送信。
    軽い判定を先に済ませ、検索するときだけChromeを起動する"""
    name = target["name"]
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    if history_data.get(f"{name}_sent_date") == today:
        print(f"[スキップ] {name} (本日送信済み)")
        return False

    h, m = map(int, target["ride_time"].split(":"))
    ride = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not (ride + timedelta(minutes=40) <= now <= ride + timedelta(hours=2)):
        print(f"[スキップ] {name} (確認時間外)")
        return False

    result, site_msg = search_bus_delay(get_driver(), target, now.date(), h, m)
    if result == "found":
        discord_post(f"[延着証明] {name} {now.strftime('%m/%d')} {target['ride_time']}頃の便",
                     ["result.png"])
        history_data[f"{name}_sent_date"] = today
    elif result == "none":
        print(f"[情報] {name} は延着なし: {site_msg}")
        if (target.get("debug") or "").strip() == "1":
            discord_post(f"[debug] {name} 延着なし判定 / サイトの表示: {site_msg}", ["result.png"])
    return True


# ==========================================
# ▼ Discordコマンド
# ==========================================
HELP_TEXT = (
    "**Commands**\n"
    "`!delay` … 今日のいつもの便の延着証明を検索\n"
    "`!delay 8:15` … 時刻を指定\n"
    "`!delay 11/4 8:15` … 日付も指定（7日以内）\n"
    "`!check <name>` … 名前の一部で指定した監視先を今すぐ確認\n"
    "`!list` … 監視先とON/OFF\n"
    "`!help` … この一覧"
)


def handle_command(driver_getter, msg_id, text, targets, history_data):
    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    now = datetime.now(JST)

    if cmd in ("help", "ヘルプ"):
        discord_post(HELP_TEXT, reply_to=msg_id)

    elif cmd in ("list", "一覧"):
        lines = []
        for t in targets:
            if not t.get("name"):
                continue
            state = "ON " if is_active(t.get("schedule"), now) else "OFF"
            sched = (t.get("schedule") or "").strip() or "always"
            lines.append(f"`{state}` {t['name']} ({(t.get('type') or 'monitor').strip()} / {sched})")
        discord_post("\n".join(lines) or "監視先がありません", reply_to=msg_id)

    elif cmd in ("delay", "遅延"):
        buses = [t for t in targets if (t.get("type") or "").strip() == "bus_delay"]
        if not buses:
            discord_post("bus_delay の行がスプレッドシートにありません", reply_to=msg_id)
            return

        day = now.date()
        hm = None
        for a in args:
            if re.match(r"^\d{1,2}:\d{2}$", a):
                hm = tuple(map(int, a.split(":")))
            elif re.match(r"^\d{1,2}/\d{1,2}$", a):
                mo, d = map(int, a.split("/"))
                year = now.year - 1 if mo > now.month else now.year
                day = date(year, mo, d)
        if not (0 <= (now.date() - day).days <= 6):
            discord_post("延着証明は7日以内の日付のみ検索できます", reply_to=msg_id)
            return

        for t in buses:
            h, m = hm or tuple(map(int, t["ride_time"].split(":")))
            result, site_msg = search_bus_delay(driver_getter(), t, day, h, m, reply_to=msg_id)
            label = f"{t['name']} {day.month}/{day.day} {h}:{m:02d}頃"
            if result == "found":
                discord_post(f"[延着証明あり] {label}", ["result.png"], msg_id)
            elif result == "none":
                discord_post(f"[延着なし] {label}\nサイトの表示: {site_msg}", ["result.png"], msg_id)

    elif cmd in ("check", "確認"):
        if not args:
            discord_post("`!check <name>` の形で指定してください", reply_to=msg_id)
            return
        hits = [t for t in targets if t.get("name") and args[0] in t["name"]
                and (t.get("type") or "monitor").strip() == "monitor"]
        if not hits:
            discord_post(f"「{args[0]}」を含む監視先が見つかりません", reply_to=msg_id)
            return
        for t in hits:
            result = check_monitor(driver_getter(), t, history_data, screenshot=True)
            msg = {"first": "初回取得しました", "changed": "変化あり", "same": "変化なし"}[result]
            discord_post(f"[確認] {t['name']}: {msg}\n{t['url']}", ["check.png"], msg_id)

    else:
        discord_post(f"「!{cmd}」は不明なコマンドです。`!help` で一覧を表示します", reply_to=msg_id)


def ensure_japanese_font():
    """スクショの日本語が□にならないよう、日本語フォントが無ければ入れる（Chromeを使う回だけ）"""
    import subprocess
    try:
        out = subprocess.run(["fc-list", ":lang=ja"], capture_output=True, text=True).stdout
        if out.strip():
            return
        cmd = ["sudo", "apt-get", "install", "-y", "-qq", "--no-install-recommends", "fonts-ipafont-gothic"]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            subprocess.run(["sudo", "apt-get", "update", "-qq"], capture_output=True)
            subprocess.run(cmd, capture_output=True)
        print("[情報] 日本語フォントをインストールしました")
    except Exception as e:
        print(f"[警告] 日本語フォントの準備に失敗: {e}")


def repo_is_private():
    """GitHub Actionsのイベント情報からリポジトリが非公開か調べる（不明ならNone）"""
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
            return json.load(f)["repository"]["private"]
    except Exception:
        return None


def build_cost_message(targets, history_data):
    def interval_of(t):
        try:
            v = float(t.get("interval", 10))
            return v if v > 0 else 10
        except ValueError:
            return 10

    # 起動間隔は「一番短いinterval（最大10分）」と見なす（workflowのcronに合わせておく）
    cron_min = min([10] + [interval_of(t) for t in targets if t.get("name")])
    runs = 30 * 24 * 60 / cron_min

    # 今から30日分を10分刻みでなぞり、scheduleで動く割合を計算する
    start = datetime.now(JST).replace(second=0, microsecond=0)
    slots = [start + timedelta(minutes=10 * i) for i in range(30 * 24 * 6)]

    lines = []
    work_sec_total = 0
    for t in targets:
        if not t.get("name"):
            continue
        name = t["name"]
        sched = (t.get("schedule") or "").strip() or "always"
        iv = interval_of(t)
        sec = history_data.get(f"{name}_measured_sec", 30)

        if (t.get("type") or "").strip() == "bus_delay":
            h, m = map(int, t["ride_time"].split(":"))
            days = {(start + timedelta(days=d)).date() for d in range(30)}
            active_days = sum(1 for d in days if is_active(
                t.get("schedule"), datetime(d.year, d.month, d.day, h, m, tzinfo=JST) + timedelta(minutes=40)))
            checks = active_days * (int(80 // iv) + 1)
            detail = f"{active_days}日 × 最大{int(80 // iv) + 1}回"
        else:
            ratio = sum(1 for x in slots if is_active(t.get("schedule"), x)) / len(slots)
            checks = 30 * 24 * 60 * ratio / iv
            detail = f"稼働{ratio * 100:.0f}% / {iv:g}分おき"

        work = checks * sec
        work_sec_total += work
        lines.append(f"- {name}: 約{int(work / 60)}分（{sched} / {detail} / 1回{sec:.0f}秒）")

    per_run_sec = 25 + work_sec_total / runs
    billed = runs * max(1, -(-per_run_sec // 60))

    private = repo_is_private()
    if private is False:
        plan = "公開リポジトリ → GitHub Actionsは無料（上限なし）"
    elif private is True:
        plan = "非公開リポジトリ → 無料枠は月約2000分" + ("　⚠️超える見込み" if billed > 2000 else "　OK")
    else:
        plan = "リポジトリの公開設定は不明"

    return (
        "[監視リスト更新]\n"
        f"{plan}\n"
        f"想定: 約{int(billed)}分/月（{int(runs)}回起動 × 1回約{per_run_sec:.0f}秒 → 1回ごとに1分単位で切り上げ）\n\n"
        "【内訳（作業時間）】\n" + "\n".join(lines)
    )


# ==========================================
# ▼ メイン処理
# ==========================================
def check_targets():
    print(f"\n[{datetime.now(JST).strftime('%H:%M:%S')}] 本番監視プロセスを開始します...")

    # スプレッドシート（CSV）の読み込み
    try:
        res = requests.get(CSV_URL)
        res.raise_for_status()
        res.encoding = 'utf-8'
        csv_text = res.text

        if not csv_text.strip() or "<html" in csv_text.lower():
            raise ValueError("CSVの内容が不正です（空データまたはエラーページを取得しました）")

        targets = list(csv.DictReader(io.StringIO(csv_text)))

        if targets and "name" not in targets[0]:
            raise ValueError("CSVに 'name' 列が見つかりません。形式が間違っています。")

    except Exception as e:
        print(f"[エラー] スプレッドシートの読み込みに失敗しました: {e}")
        return

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
        print("[情報] スプレッドシートの変更を検知しました。コスト再計算を予約します。")

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--log-level=3')
    options.add_argument('--window-size=800,1400')
    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])

    driver_box = {"d": None}

    def get_driver():
        if driver_box["d"] is None:
            ensure_japanese_font()
            driver_box["d"] = webdriver.Chrome(options=options)
        return driver_box["d"]

    # ---- Discordに届いたコマンドを先に処理する ----
    for msg_id, text in fetch_new_commands(history_data):
        print(f"[コマンド] {text}")
        try:
            handle_command(get_driver, msg_id, text, targets, history_data)
        except Exception as e:
            print(f"[エラー] コマンド処理中にエラー: {e}")
            discord_post(f"[エラー] コマンド処理に失敗しました: {e}", reply_to=msg_id)

    # ---- 定期チェック ----
    now = datetime.now(JST)
    for target in targets:
        if not target.get('name') or not target.get('url'):
            continue

        target_name = target['name']
        target_type = (target.get('type') or 'monitor').strip()

        if not is_active(target.get('schedule'), now):
            print(f"[スキップ] {target_name} (スケジュールでOFF)")
            continue

        try:
            interval_minutes = float(target.get('interval', 10))
        except ValueError:
            interval_minutes = 10

        last_run_time = history_data.get(f"{target_name}_last_run", 0)
        current_time = time.time()

        required_elapsed_seconds = max(0, (interval_minutes * 60) - 150)
        if current_time - last_run_time < required_elapsed_seconds:
            print(f"[スキップ] {target_name} (設定: {interval_minutes}分おき)")
            continue

        print(f"\n[確認中] {target_name} ({target_type})")
        target_start_time = time.time()

        did_work = True
        try:
            if target_type == "bus_delay":
                did_work = auto_bus_check(get_driver, target, history_data)
            else:
                result = check_monitor(get_driver(), target, history_data)
                if result == "changed":
                    send_discord(f"[検知] {target_name} で内容に変化がありました。\nURL: {target['url']}")

            history_data[f"{target_name}_last_run"] = current_time

        except Exception as e:
            print(f"[エラー] {target_name} の確認中にエラー発生: {e}")
            if target_type == "bus_delay":
                debug_dump(get_driver(), target_name, f"例外: {e}")

        if did_work:
            history_data[f"{target_name}_measured_sec"] = time.time() - target_start_time

    if driver_box["d"] is not None:
        driver_box["d"].quit()

    # ==========================================
    # ▼ コスト計算（scheduleを反映 / 1回の起動ごとに1分単位で切り上げて課金される）
    # ==========================================
    if csv_changed:
        cost_message = build_cost_message(targets, history_data)
        send_discord(cost_message)
        print(cost_message)

    # ==========================================
    # ▼ 不要になった過去データをクレンジング
    # ==========================================
    cleaned_history = {}
    for key in ("csv_hash", "discord_last_msg_id"):
        if key in history_data:
            cleaned_history[key] = history_data[key]

    for target in targets:
        name = target.get('name')
        if not name:
            continue
        for key in (name, f"{name}_last_run", f"{name}_measured_sec", f"{name}_sent_date"):
            if key in history_data:
                cleaned_history[key] = history_data[key]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned_history, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    check_targets()
