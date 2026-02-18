import os
import re
import time
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import threading
from flask import Flask

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot is running", 200

def run_web():
    import os
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# Start web server in background
threading.Thread(target=run_web, daemon=True).start()


BOT_TOKEN = "8515346347:AAFts8VLh4GiIbkonWrHdqp-uPwfaG5dkPU"
if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")

TARGET_CHAT_ID = None  # Example: -1001234567890

ESPN_SCOREBOARD_URLS = [
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/scoreboard",
]

ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/schedule"

ABBR_FIX = {
    "GS": "GSW",
    "SA": "SAS",
    "NY": "NYK",
    "WSH": "WAS",
    "NO": "NOP",
    "PHO": "PHX",
    "BRK": "BKN",
    "CHO": "CHA",
}

HTTP_TIMEOUT = 20
MAX_RETRIES = 4

@dataclass
class Matchup:
    away_abbr: str
    home_abbr: str

def normalize_abbr(abbr: str) -> str:
    abbr = (abbr or "").upper().strip()
    return ABBR_FIX.get(abbr, abbr)

def utc_today() -> dt.date:
    return dt.datetime.utcnow().date()

def get_tomorrow_utc() -> dt.date:
    return utc_today() + dt.timedelta(days=1)

def parse_date_arg(arg: Optional[str]) -> Optional[dt.date]:
    if not arg:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
        return None
    y, m, d = map(int, arg.split("-"))
    return dt.date(y, m, d)

def date_to_yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")

def espn_season_year_for_date(d: dt.date) -> int:
    return d.year if d.month >= 10 else d.year - 1

# ---- requests session + retries ----
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NBA-Predictions-Bot/1.0"
})

def get_json_with_retries(url: str, params: Optional[dict] = None) -> dict:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(min(2 ** attempt, 10))
    raise last_err

# ---- team map cache ----
TEAM_MAP_CACHE: Optional[Dict[str, dict]] = None
TEAM_MAP_CACHE_TS: float = 0.0
TEAM_MAP_TTL_SECONDS = 6 * 60 * 60

def load_team_map() -> Dict[str, dict]:
    global TEAM_MAP_CACHE, TEAM_MAP_CACHE_TS
    now = time.time()

    if TEAM_MAP_CACHE and (now - TEAM_MAP_CACHE_TS) < TEAM_MAP_TTL_SECONDS:
        return TEAM_MAP_CACHE

    data = get_json_with_retries(ESPN_TEAMS_URL)
    sports = data.get("sports", [])
    leagues = sports[0].get("leagues", []) if sports else []
    teams_list = leagues[0].get("teams", []) if leagues else []

    m: Dict[str, dict] = {}
    for item in teams_list:
        team = (item or {}).get("team", {}) or {}
        tid = team.get("id")
        abbr = normalize_abbr(team.get("abbreviation", ""))
        name = team.get("shortDisplayName") or team.get("displayName") or abbr
        if tid and abbr:
            m[abbr] = {"id": int(tid), "name": str(name)}

    if not m:
        raise RuntimeError("Failed to load team list from ESPN.")

    TEAM_MAP_CACHE = m
    TEAM_MAP_CACHE_TS = now
    return m

# ---- schedule wins cache ----
SCHEDULE_WINS_CACHE: Dict[Tuple[int, int, str], List[bool]] = {}

def fetch_team_recent_wins(team_id: int, season_year: int, cutoff_date: dt.date) -> List[bool]:
    cutoff_key = date_to_yyyymmdd(cutoff_date)
    cache_key = (team_id, season_year, cutoff_key)
    if cache_key in SCHEDULE_WINS_CACHE:
        return SCHEDULE_WINS_CACHE[cache_key]

    params = {"season": season_year}
    url = ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id)
    data = get_json_with_retries(url, params=params)

    events = data.get("events", []) or []
    wins: List[Tuple[dt.datetime, bool]] = []

    for ev in events:
        date_str = ev.get("date")
        if not date_str:
            continue

        try:
            game_dt = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            continue

        if game_dt.date() >= cutoff_date:
            continue

        comps = ev.get("competitions", [])
        if not comps:
            continue

        comp = comps[0]
        status = comp.get("status", {}).get("type", {}) or {}
        if not bool(status.get("completed")):
            continue

        competitors = comp.get("competitors", []) or []
        team_won = None
        for c in competitors:
            t = c.get("team", {}) or {}
            tid = t.get("id")
            if tid is None:
                continue
            if int(tid) == int(team_id):
                team_won = bool(c.get("winner"))
                break

        if team_won is None:
            continue

        wins.append((game_dt, team_won))

    wins.sort(key=lambda x: x[0], reverse=True)
    win_flags = [w for _, w in wins]
    SCHEDULE_WINS_CACHE[cache_key] = win_flags
    return win_flags

def record_last_n(team_id: int, season_year: int, cutoff_date: dt.date, n: int) -> Tuple[int, int]:
    win_flags = fetch_team_recent_wins(team_id, season_year, cutoff_date)
    sample = win_flags[:n]
    w = sum(1 for x in sample if x)
    l = len(sample) - w
    return w, l

def fetch_scoreboard_games_utc(game_date_utc: dt.date) -> List[Matchup]:
    params = {"dates": date_to_yyyymmdd(game_date_utc)}
    last_error = None

    for url in ESPN_SCOREBOARD_URLS:
        try:
            data = get_json_with_retries(url, params=params)
            games: List[Matchup] = []
            events = data.get("events", []) or []
            for ev in events:
                comps = ev.get("competitions", [])
                if not comps:
                    continue
                competitors = comps[0].get("competitors", []) or []
                home = away = None

                for c in competitors:
                    team = c.get("team", {}) or {}
                    abbr = normalize_abbr(team.get("abbreviation", ""))
                    if c.get("homeAway") == "home":
                        home = abbr
                    elif c.get("homeAway") == "away":
                        away = abbr

                if home and away:
                    games.append(Matchup(away_abbr=away, home_abbr=home))

            return games
        except Exception as e:
            last_error = e

    raise last_error if last_error else RuntimeError("Error fetching ESPN scoreboard")

def pick_winner(away_abbr: str, home_abbr: str, cutoff_date: dt.date) -> Tuple[str, dict]:
    away = normalize_abbr(away_abbr)
    home = normalize_abbr(home_abbr)

    team_map = load_team_map()
    if away not in team_map:
        raise ValueError(f"Unknown team abbreviation: {away}")
    if home not in team_map:
        raise ValueError(f"Unknown team abbreviation: {home}")

    away_id = team_map[away]["id"]
    home_id = team_map[home]["id"]
    season_year = espn_season_year_for_date(cutoff_date)

    a10w, a10l = record_last_n(away_id, season_year, cutoff_date, 10)
    h10w, h10l = record_last_n(home_id, season_year, cutoff_date, 10)

    if a10w != h10w:
        winner = away if a10w > h10w else home
        reason = "last10"
        a5 = h5 = None
    else:
        a5w, a5l = record_last_n(away_id, season_year, cutoff_date, 5)
        h5w, h5l = record_last_n(home_id, season_year, cutoff_date, 5)

        if a5w != h5w:
            winner = away if a5w > h5w else home
            reason = "last5"
        else:
            winner = home
            reason = "home_tiebreak"

        a5 = (a5w, a5l)
        h5 = (h5w, h5l)

    return winner, {
        "away": away,
        "home": home,
        "away10": (a10w, a10l),
        "home10": (h10w, h10l),
        "away5": a5,
        "home5": h5,
        "reason": reason,
    }

def format_prediction(winner: str, info: dict) -> str:
    away = info["away"]
    home = info["home"]
    line = f"{away} @ {home}  →  🏆 *{winner}*"

    a10w, a10l = info["away10"]
    h10w, h10l = info["home10"]
    line += f"\nLast10: {away} {a10w}-{a10l} | {home} {h10w}-{h10l}"

    if info["away5"] and info["home5"]:
        a5w, a5l = info["away5"]
        h5w, h5l = info["home5"]
        line += f"\nLast5:  {away} {a5w}-{a5l} | {home} {h5w}-{h5l}"

    if info["reason"] == "home_tiebreak":
        line += "\nTie-break: home team"

    return line

async def send_text(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if TARGET_CHAT_ID is not None:
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=text, parse_mode="Markdown")
    else:
        if update and update.message:
            await update.message.reply_text(text, parse_mode="Markdown")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(update, context,
        "Send /preds to get tomorrow's NBA predictions (UTC).\n"
        "Or: /preds YYYY-MM-DD (UTC date)\n\n"
        "Logic: last10 → last5 → home.\n"
        "Winner has 🏆."
    )

async def preds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = context.args[0] if context.args else None
    d = parse_date_arg(arg) if arg else get_tomorrow_utc()
    if d is None:
        await send_text(update, context, "Use: /preds or /preds YYYY-MM-DD (UTC)")
        return

    try:
        games = fetch_scoreboard_games_utc(d)
    except Exception as e:
        await send_text(update, context, f"Error fetching schedule: {e}")
        return

    if not games:
        await send_text(update, context, f"No NBA games found for {d} (UTC).")
        return

    season_year = espn_season_year_for_date(d)
    out_lines = [f"🏀 NBA predictions for *{d} (UTC)*\nSeason start year: {season_year}\n"]

    for g in games:
        try:
            winner, info = pick_winner(g.away_abbr, g.home_abbr, cutoff_date=d)
            out_lines.append(format_prediction(winner, info))
        except Exception as e:
            out_lines.append(f"{normalize_abbr(g.away_abbr)} @ {normalize_abbr(g.home_abbr)}  →  (error: {e})")

    text = "\n\n".join(out_lines)
    MAX = 3500
    if len(text) <= MAX:
        await send_text(update, context, text)
    else:
        for i in range(0, len(text), MAX):
            await send_text(update, context, text[i:i+MAX])

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("preds", preds_cmd))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

