import os
import json
import datetime as dt
import requests
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

# =========================
# CONFIG (set these as ENV vars)
# =========================
# Windows PowerShell example:
#   setx BOT_TOKEN "YOUR_NEW_TOKEN"
#   setx CHAT_ID "-1001234567890"
#   setx APIFOOTBALL_KEY "YOUR_KEY"
#   setx SEASON "2025"

BOT_TOKEN = "8015747952:AAHtT-QYnvzKkdoaw-CbAZX_3RMWaIO0Fv8"
CHAT_ID = 6003298116
CHAT_ID = -1003556108331

TZ = ZoneInfo("Asia/Riyadh")

STATE_DIR = "state"
os.makedirs(STATE_DIR, exist_ok=True)
FPL_SEEN_FILE = os.path.join(STATE_DIR, "fpl_weekly_seen.json")
LIVE_SEEN_FILE = os.path.join(STATE_DIR, "live_seen_events.json")

LAST_N_GWS = 5
TOP_N = 5

# Anti-cameo filters (adjust if you want)
MIN_APPS_LAST5 = int(os.getenv("MIN_APPS_LAST5", "2"))        # set 1 if you want
MIN_TOTAL_MIN_LAST5 = int(os.getenv("MIN_TOTAL_MIN_LAST5", "0"))

# =========================
# LIVE EVENTS API (API-FOOTBALL / API-Sports) - EPL ONLY
# =========================
APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "")
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
EPL_LEAGUE_ID = 39
SEASON = os.getenv("SEASON", "")  # IMPORTANT: set explicitly e.g. "2025"
LIVE_POLL_SECONDS = int(os.getenv("LIVE_POLL_SECONDS", "45"))

# =========================
# FPL API
# =========================
FPL_BASE = "https://fantasy.premierleague.com/api"

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (TelegramBot)"})


# =========================
# State helpers
# =========================
def load_set(path: str) -> set:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_set(path: str, s: set, keep_last: int = 5000):
    arr = list(s)
    if len(arr) > keep_last:
        arr = arr[-keep_last:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(arr, f)


def get_first(d: dict, keys: list[str], default=0):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


# =========================
# FPL fetch
# =========================
def fpl_bootstrap():
    r = session.get(f"{FPL_BASE}/bootstrap-static/", timeout=30)
    r.raise_for_status()
    return r.json()


def fpl_element_summary(element_id: int):
    r = session.get(f"{FPL_BASE}/element-summary/{element_id}/", timeout=30)
    r.raise_for_status()
    return r.json()


# =========================
# Weekly (last 5 GWs) calculations
# =========================
def compute_weekly_fpl(top_n=5, last_n=5, min_apps=2, min_total_min=0):
    """
    Returns:
      top_by_pos[pos] = [(ppa, pts, apps, mins, name, team), ...]  (GK/DEF/MID/FWD)
      top_defcon[pos] = [(dc_pa, dc_sum, dc_pts, apps, name, team), ...] (DEF/MID/FWD only)
    """
    boot = fpl_bootstrap()
    elements = boot["elements"]
    teams = {t["id"]: t["name"] for t in boot["teams"]}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    top_by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    top_defcon = {"DEF": [], "MID": [], "FWD": []}

    for p in elements:
        pos = pos_map.get(p.get("element_type"))
        if not pos:
            continue

        pid = p["id"]
        name = f'{p.get("first_name","")} {p.get("second_name","")}'.strip()
        team = teams.get(p.get("team"), "Unknown")

        try:
            hist = fpl_element_summary(pid).get("history", [])
        except Exception:
            continue

        if not hist:
            continue

        last = hist[-last_n:]

        pts = sum(x.get("total_points", 0) for x in last)
        mins = sum(x.get("minutes", 0) for x in last)
        apps = sum(1 for x in last if x.get("minutes", 0) > 0)

        if apps < min_apps:
            continue
        if mins < min_total_min:
            continue

        ppa = pts / apps
        top_by_pos[pos].append((ppa, pts, apps, mins, name, team))

        if pos in top_defcon:
            dc_sum = sum(get_first(x, ["defensive_contribution", "defcon"], 0) for x in last)
            dc_pts = sum(get_first(x, ["defensive_contribution_points", "defcon_points"], 0) for x in last)
            dc_pa = dc_sum / apps
            top_defcon[pos].append((dc_pa, dc_sum, dc_pts, apps, name, team))

    for pos in top_by_pos:
        top_by_pos[pos].sort(reverse=True, key=lambda x: (x[0], x[1], x[2], x[3]))
        top_by_pos[pos] = top_by_pos[pos][:top_n]

    for pos in top_defcon:
        top_defcon[pos].sort(reverse=True, key=lambda x: (x[0], x[1], x[2], x[3]))
        top_defcon[pos] = top_defcon[pos][:top_n]

    return top_by_pos, top_defcon


# =========================
# Thursday: last finished GW (Top performers + price)
# =========================
def get_last_finished_gw(bootstrap_json: dict) -> int:
    events = bootstrap_json.get("events", [])
    finished = [e["id"] for e in events if e.get("finished")]
    if finished:
        return max(finished)

    current = next((e["id"] for e in events if e.get("is_current")), None)
    if current and current > 1:
        return current - 1
    return 1


def format_price(now_cost: int) -> str:
    # FPL now_cost is usually in tenths (e.g. 75 = 7.5)
    return f"{now_cost / 10:.1f}"


def compute_top_last_gw_by_position(top_n=5, min_minutes=1):
    boot = fpl_bootstrap()
    last_gw = get_last_finished_gw(boot)

    teams = {t["id"]: t["name"] for t in boot["teams"]}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    buckets = {"GK": [], "DEF": [], "MID": [], "FWD": []}

    for p in boot["elements"]:
        pos = pos_map.get(p.get("element_type"))
        if not pos:
            continue

        pid = p["id"]
        name = f'{p.get("first_name","")} {p.get("second_name","")}'.strip()
        team = teams.get(p.get("team"), "Unknown")
        price = format_price(p.get("now_cost", 0))

        try:
            hist = fpl_element_summary(pid).get("history", [])
        except Exception:
            continue

        gw_row = next((h for h in hist if h.get("round") == last_gw), None)
        if not gw_row:
            continue

        mins = gw_row.get("minutes", 0)
        if mins < min_minutes:
            continue

        pts = gw_row.get("total_points", 0)
        buckets[pos].append((pts, mins, name, team, price))

    for pos in buckets:
        buckets[pos].sort(reverse=True, key=lambda x: (x[0], x[1]))
        buckets[pos] = buckets[pos][:top_n]

    return last_gw, buckets


# =========================
# Message builders (SPLIT MESSAGES)
# =========================
def build_pos_message(today_iso: str, pos: str, items) -> str:
    lines = [
        f"🏆 FPL — Top {TOP_N} {pos} (last {LAST_N_GWS} GWs) — {today_iso}",
        "Ranking: Points per Appearance (apps = minutes>0)",
        f"Filter: min apps = {MIN_APPS_LAST5}\n",
    ]

    if not items:
        lines.append("No players matched the filter.")
        return "\n".join(lines)

    for i, (ppa, pts, apps, mins, name, team) in enumerate(items, 1):
        lines.append(f"{i}) {name} ({team}) — {ppa:.2f} P/A | {pts} pts | {apps} apps")

    return "\n".join(lines)


def build_defcon_message(today_iso: str, pos: str, items) -> str:
    lines = [
        f"🛡️ FPL — Defensive Contributions: Top {TOP_N} {pos} (last {LAST_N_GWS} GWs) — {today_iso}",
        "Ranking: Defensive Contributions per Appearance\n",
    ]

    if not items:
        lines.append("No players matched the filter.")
        return "\n".join(lines)

    for i, (dc_pa, dc_sum, dc_pts, apps, name, team) in enumerate(items, 1):
        pts_txt = f" | DefCon pts: {dc_pts}" if dc_pts else ""
        lines.append(f"{i}) {name} ({team}) — {dc_pa:.2f} DC/A | DC: {dc_sum} | apps: {apps}{pts_txt}")

    return "\n".join(lines)


def build_last_gw_message(today_iso: str, gw: int, pos: str, items: list) -> str:
    lines = [
        f"🔥 FPL — Top {TOP_N} {pos} (GW{gw}) — {today_iso}",
        "Ranking: points in last finished GW\n",
    ]

    if not items:
        lines.append("No players found.")
        return "\n".join(lines)

    for i, (pts, mins, name, team, price) in enumerate(items, 1):
        lines.append(f"{i}) {name} ({team}) — {pts} pts | {mins} mins | £{price}m")

    return "\n".join(lines)


async def send_weekly_split_messages(bot, chat_id: int, today_iso: str, top_by_pos, top_defcon):
    for pos in ["GK", "DEF", "MID", "FWD"]:
        msg = build_pos_message(today_iso, pos, top_by_pos.get(pos, []))
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

    for pos in ["DEF", "MID", "FWD"]:
        msg = build_defcon_message(today_iso, pos, top_defcon.get(pos, []))
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


async def send_last_gw_split_messages(bot, chat_id: int, today_iso: str):
    gw, buckets = compute_top_last_gw_by_position(top_n=TOP_N, min_minutes=1)

    # You asked: strikers + price, same for MID/DEF/GK
    for pos in ["FWD", "MID", "DEF", "GK"]:
        msg = build_last_gw_message(today_iso, gw, pos, buckets.get(pos, []))
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")


# =========================
# Scheduled posts
# =========================
async def post_fpl_weekly_tuesday(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID == 0:
        return

    today = dt.datetime.now(TZ).date().isoformat()
    seen = load_set(FPL_SEEN_FILE)
    key = f"tuesday:{today}"
    if key in seen:
        return

    top_by_pos, top_defcon = compute_weekly_fpl(
        top_n=TOP_N,
        last_n=LAST_N_GWS,
        min_apps=MIN_APPS_LAST5,
        min_total_min=MIN_TOTAL_MIN_LAST5,
    )

    await send_weekly_split_messages(context.bot, CHAT_ID, today, top_by_pos, top_defcon)

    seen.add(key)
    save_set(FPL_SEEN_FILE, seen)


async def post_last_gw_thursday(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID == 0:
        return

    today = dt.datetime.now(TZ).date().isoformat()
    await send_last_gw_split_messages(context.bot, CHAT_ID, today)


# =========================
# LIVE GOALS (EPL ONLY)
# =========================
def api_headers():
    if not APIFOOTBALL_KEY:
        raise RuntimeError("Missing APIFOOTBALL_KEY")
    return {"x-apisports-key": APIFOOTBALL_KEY}


def apifootball_live_fixtures():
    r = session.get(
        f"{APIFOOTBALL_BASE}/fixtures",
        headers=api_headers(),
        params={"live": "all", "league": EPL_LEAGUE_ID, "season": SEASON},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def apifootball_fixture_events(fixture_id: int):
    r = session.get(
        f"{APIFOOTBALL_BASE}/fixtures/events",
        headers=api_headers(),
        params={"fixture": fixture_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def post_live_events(context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID == 0 or not APIFOOTBALL_KEY or not SEASON:
        return

    seen = load_set(LIVE_SEEN_FILE)

    try:
        live = apifootball_live_fixtures()
    except Exception:
        return

    fixtures = live.get("response", [])
    for fx in fixtures:
        fixture_id = fx["fixture"]["id"]
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        score_home = fx["goals"]["home"]
        score_away = fx["goals"]["away"]

        try:
            events = apifootball_fixture_events(fixture_id).get("response", [])
        except Exception:
            continue

        for ev in events:
            if ev.get("type") != "Goal":
                continue

            minute = ev.get("time", {}).get("elapsed", "?")
            team = ev.get("team", {}).get("name", "")
            scorer = ev.get("player", {}).get("name", "Unknown")
            assist = ev.get("assist", {}).get("name")
            detail = ev.get("detail", "")

            ev_key = f"{fixture_id}:{minute}:{scorer}:{assist}:{detail}"
            if ev_key in seen:
                continue

            assist_txt = f" (assist: {assist})" if assist else ""
            text = (
                f"⚽ {home} {score_home}-{score_away} {away}\n"
                f"{minute}' — {team}: {scorer}{assist_txt}"
            )
            await context.bot.send_message(chat_id=CHAT_ID, text=text)

            seen.add(ev_key)

    save_set(LIVE_SEEN_FILE, seen)


# =========================
# Commands
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot is running.\n"
        "Commands:\n"
        "• /chatid    (shows chat id)\n"
        "• /test      (run Tuesday split posts now)\n"
        "• /test_gw   (run Thursday last-GW posts now)\n\n"
        "Schedules (Riyadh):\n"
        "• Tuesday 12:00: last-5 GWs (positions + DefCon)\n"
        "• Thursday 12:00: last finished GW top performers + price\n"
        "Live goals: needs APIFOOTBALL_KEY + SEASON."
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat_id = {update.effective_chat.id}")


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID == 0:
        await update.message.reply_text("❌ CHAT_ID is 0. Set CHAT_ID env var and restart the bot.")
        return

    await update.message.reply_text("✅ Running Tuesday-style split posts now...")
    today = dt.datetime.now(TZ).date().isoformat()
    top_by_pos, top_defcon = compute_weekly_fpl(
        top_n=TOP_N,
        last_n=LAST_N_GWS,
        min_apps=MIN_APPS_LAST5,
        min_total_min=MIN_TOTAL_MIN_LAST5,
    )
    await send_weekly_split_messages(context.bot, CHAT_ID, today, top_by_pos, top_defcon)


async def test_gw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CHAT_ID == 0:
        await update.message.reply_text("❌ CHAT_ID is 0. Set CHAT_ID env var and restart the bot.")
        return

    await update.message.reply_text("✅ Running Thursday last-GW posts now...")
    today = dt.datetime.now(TZ).date().isoformat()
    await send_last_gw_split_messages(context.bot, CHAT_ID, today)


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN. Set env BOT_TOKEN and restart.")

    defaults = Defaults(tzinfo=TZ)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("test_gw", test_gw))

    # Tuesday 12:00 Riyadh (Mon=0, Tue=1)
    app.job_queue.run_daily(
        post_fpl_weekly_tuesday,
        time=dt.time(hour=12, minute=0, tzinfo=TZ),
        days=(1,),
    )

    # Thursday 12:00 Riyadh (Thu=3)
    app.job_queue.run_daily(
        post_last_gw_thursday,
        time=dt.time(hour=12, minute=0, tzinfo=TZ),
        days=(3,),
    )

    # Live goals polling
    app.job_queue.run_repeating(post_live_events, interval=LIVE_POLL_SECONDS, first=10)

    app.run_polling()


if __name__ == "__main__":
    main()
