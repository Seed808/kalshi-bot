"""Shared config, time, HTTP and math helpers."""
import copy
import json
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
    HST = ZoneInfo("Pacific/Honolulu")
except Exception:  # pragma: no cover
    HST = timezone(timedelta(hours=-10))

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
ODDS = "https://api.the-odds-api.com/v4"

DEFAULTS = {
    # ---- what triggers a paper trade
    #   "picks": back the sportsbook favorite on every matched game (see picks.py), fixed $ stake
    #   "edge":  only when Kalshi is >= min_edge cheaper than the sportsbook consensus after fees
    "mode": "picks",
    "pick_stake_dollars": 10.0,
    "pick_upset_rate": 0.25,         # chance of taking the underdog instead of the favorite
    "pick_min_win_chance": 0.55,     # slate floor: drop picks below this win chance...
    "pick_value_underdog_min_return": 0.08,  # ...unless a sportsbook-priced underdog returns >= 8%/$
    "pick_injury_override": True,    # strong injury news on the favorite -> take the underdog
    "pick_min_books": 2,             # sportsbooks needed for a consensus in pick mode
    "pick_kalshi_fallback": True,    # no sportsbook line? use Kalshi's own prices to find the favorite
    "pick_kalshi_max_spread": 0.10,  # ...only if both teams' bid/ask gaps are this tight or tighter
    # ---- markets: Kalshi game-winner series -> The Odds API sport key (VERIFY with `series`)
    "series": {   # Kalshi game-winner series -> Odds API key (None = no sportsbook feed: Kalshi prices only)
        # football
        "KXNFLGAME": "americanfootball_nfl",
        "KXNCAAFGAME": "americanfootball_ncaaf",
        "KXCFLGAME": "americanfootball_cfl",
        # basketball
        "KXNBAGAME": "basketball_nba",
        "KXNCAAMBGAME": "basketball_ncaab",
        "KXNCAAWBGAME": "basketball_wncaab",
        "KXWNBAGAME": "basketball_wnba",
        # baseball / softball
        "KXMLBGAME": "baseball_mlb",
        "KXNCAABBGAME": "baseball_ncaa",
        "KXNCAASBGAME": None,
        # hockey
        "KXNHLGAME": "icehockey_nhl",
        "KXNCAAHGAME": None,
        # soccer (three outcomes: the bot picks one of the two teams to win)
        "KXMLSGAME": "soccer_usa_mls",
        "KXNWSLGAME": None,
        "KXEPLGAME": "soccer_epl",
        "KXUCLGAME": "soccer_uefa_champs_league",
        "KXLALIGAGAME": "soccer_spain_la_liga",
        "KXBUNDESLIGAGAME": "soccer_germany_bundesliga",
        "KXSERIEAGAME": "soccer_italy_serie_a",
        "KXLIGUE1GAME": "soccer_france_ligue_one",
        "KXLIGAMXGAME": "soccer_mexico_ligamx",
        # combat sports
        "KXUFCFIGHT": "mma_mixed_martial_arts",
        "KXBOXINGFIGHT": "boxing_boxing",
        # tennis: Odds API has one key per tournament; "*" = every active key with that prefix
        "KXATPMATCH": "tennis_atp_*",
        "KXWTAMATCH": "tennis_wta_*",
        # other college
        "KXNCAAWVMATCH": None,
    },
    "sport_labels": {
        "KXNFLGAME": "NFL", "KXNCAAFGAME": "NCAAFB", "KXCFLGAME": "CFL",
        "KXNBAGAME": "NBA", "KXNCAAMBGAME": "NCAA Men's Basketball",
        "KXNCAAWBGAME": "NCAA Women's Basketball", "KXWNBAGAME": "WNBA",
        "KXMLBGAME": "MLB", "KXNCAABBGAME": "NCAA Baseball", "KXNCAASBGAME": "NCAA Softball",
        "KXNHLGAME": "NHL", "KXNCAAHGAME": "NCAA Hockey",
        "KXMLSGAME": "MLS", "KXNWSLGAME": "NWSL", "KXEPLGAME": "Premier League",
        "KXUCLGAME": "Champions League", "KXLALIGAGAME": "La Liga", "KXBUNDESLIGAGAME": "Bundesliga",
        "KXSERIEAGAME": "Serie A", "KXLIGUE1GAME": "Ligue 1", "KXLIGAMXGAME": "Liga MX",
        "KXUFCFIGHT": "UFC", "KXBOXINGFIGHT": "Boxing",
        "KXATPMATCH": "Tennis (ATP)", "KXWTAMATCH": "Tennis (WTA)",
        "KXNCAAWVMATCH": "NCAA Women's Volleyball",
    },
    # rough hours from start to Kalshi's expected end; used to estimate start time when only
    # Kalshi prices are available, so the bot never picks a game that may already be under way
    "game_hours": {"default": 3.0, "soccer": 2.0, "KXUFCFIGHT": 6.0, "KXBOXINGFIGHT": 6.0},
    "soccer_series": ["KXMLSGAME", "KXNWSLGAME", "KXEPLGAME", "KXUCLGAME", "KXLALIGAGAME",
                      "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME", "KXLIGAMXGAME"],
    "individual_series": ["KXATPMATCH", "KXWTAMATCH", "KXUFCFIGHT", "KXBOXINGFIGHT"],  # people, not teams
    "odds_spend_horizon_hours": 36,   # only spend an odds credit on a sport with a game ending within this
    # ---- step 1: scan agent (all Kalshi markets)
    "scan_min_volume_24h": 500,
    "scan_max_hours": 72,
    "scan_move_threshold": 0.10,
    "scan_wide_spread": 0.10,
    "scan_max_pages": 10,
    "alert_anomalies": False,
    # ---- odds / pricing
    "odds_regions": "us",            # 1 region x 1 market (h2h) = 1 credit per sport call
    "odds_monthly_credit_budget": 450,   # free plan is 500/month; keep a buffer
    "odds_credit_reserve": 25,
    "odds_max_credits_per_day": 15,  # ~450/30; stops early-month burn       # never go below this many remaining (per API header)
    "odds_cache_min": 180,           # reuse odds this long before spending another credit
    "bookmakers": [],
    "min_books": 3,
    "max_book_std": 0.04,
    "max_odds_staleness_min": 30,
    "max_hours_ahead": 36,
    "match_threshold": 0.5,
    # ---- step 2: research agents
    "rss_feeds": {
        "KXNFLGAME": ["https://www.espn.com/espn/rss/nfl/news"],
        "KXNBAGAME": ["https://www.espn.com/espn/rss/nba/news"],
        "KXMLBGAME": ["https://www.espn.com/espn/rss/mlb/news"],
        "KXNHLGAME": ["https://www.espn.com/espn/rss/nhl/news"],
        "KXNCAAFGAME": ["https://www.espn.com/espn/rss/ncf/news"],
    },
    "subreddits": {
        "KXNFLGAME": ["nfl", "fantasyfootball"],
        "KXNBAGAME": ["nba", "fantasybball"],
        "KXMLBGAME": ["baseball", "fantasybaseball"],
        "KXNHLGAME": ["hockey", "fantasyhockey"],
        "KXNCAAFGAME": ["CFB"],
        "KXATPMATCH": ["tennis"],
        "KXWTAMATCH": ["tennis"],
    },
    "research_lookback_hours": 24,
    "research_ttl_min": 60,
    "x_enabled": False,              # X is PAID ($0.005/read) — off to keep the bot free
    "x_max_results": 10,
    "x_monthly_read_cap": 0,         # hard cap on paid X reads; 0 = none
    "injury_veto_count": 2,
    # ---- step 3: prediction agent
    "min_edge": 0.03,
    "series_min_edge": {},
    "max_spread": 0.06,
    "min_size": 25,
    "model_min_samples": 400,
    "model_min_improvement": 0.002,
    "retrain_every": 50,
    # ---- step 4: risk agent (PAPER)
    "bankroll": 1000.0,
    "kelly_fraction": 0.25,
    "max_stake_pct": 0.05,
    "max_open_exposure_pct": 0.6,
    "max_open_trades": 15,
    "max_trades_per_day": 15,
    "daily_loss_limit_pct": 0.25,
    # ---- step 5: postmortem / learning
    "learn_window": 40,
    "blocked_series": [],
    # ---- files
    "db_path": "kalshibot.db",
    "overrides_path": "learned_overrides.json",
    "model_path": "model.json",
    "model_meta_path": "model_meta.json",
}

TIGHTEN_KEYS = ("min_edge", "max_odds_staleness_min", "injury_veto_count")


def load_config(path="config.json"):
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    ov = load_overrides(cfg)
    for k in TIGHTEN_KEYS:
        if k in ov:
            cfg[k] = ov[k]
    cfg["series_min_edge"].update(ov.get("series_min_edge", {}))
    cfg["blocked_series"] = sorted(set(cfg["blocked_series"]) | set(ov.get("blocked_series", [])))
    return cfg


def load_overrides(cfg):
    p = cfg["overrides_path"]
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_overrides(cfg, ov):
    with open(cfg["overrides_path"], "w") as f:
        json.dump(ov, f, indent=2)


def now_utc():
    return datetime.now(timezone.utc)


def hst(dt):
    return dt.astimezone(HST).strftime("%a %m/%d %I:%M %p HST")


def hst_date(dt):
    return dt.astimezone(HST).date()


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


SECRET_ENV = ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL",
              "NTFY_TOPIC", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "X_BEARER_TOKEN")


class _Redactor:
    """Wraps stdout/stderr so secrets can't leak into logs (incl. the committed last_run.log)."""

    def __init__(self, stream):
        self._s = stream
        self._secrets = [v for k, v in os.environ.items() if k in SECRET_ENV and v and len(v) >= 6]

    def write(self, t):
        for v in self._secrets:
            t = t.replace(v, "***")
        t = re.sub(r"(apiKey=)[^&\s'\"]+", r"\1***", t)
        t = re.sub(r"(api\.telegram\.org/bot)[^/\s]+", r"\1***", t)
        return self._s.write(t)

    def flush(self):
        self._s.flush()

    def __getattr__(self, name):
        return getattr(self._s, name)


def install_redaction():
    import sys
    if not isinstance(sys.stdout, _Redactor):
        sys.stdout, sys.stderr = _Redactor(sys.stdout), _Redactor(sys.stderr)


_SESSION = requests.Session()


def http_get(url, params=None, headers=None, retries=3):
    for i in range(retries):
        try:
            r = _SESSION.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    raise RuntimeError(f"GET failed after retries: {url}")


# ---------------------------------------------------------------- math

def fee_per_contract(price, multiplier=1.0):
    """Kalshi taker fee per contract before rounding: 0.07 * P * (1-P) * multiplier."""
    return 0.07 * multiplier * price * (1 - price)


def order_fee(price, contracts, multiplier=1.0):
    """Order-level taker fee, rounded up to the next cent."""
    raw = 0.07 * multiplier * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100


def american_to_decimal(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def devig_two_way(dec_a, dec_b):
    ia, ib = 1 / dec_a, 1 / dec_b
    s = ia + ib
    return ia / s, ib / s


def kelly_binary(q, cost):
    """Kelly fraction for paying `cost` for a $1 payout with win probability q."""
    if cost <= 0 or cost >= 1:
        return 0.0
    return max(0.0, (q - cost) / (1 - cost))


# ---------------------------------------------------------------- matching

_STOP = {"the", "fc", "sc", "university", "of"}


_ALIAS = {"st": "state"}   # Kalshi writes "Texas St.", sportsbooks "Texas State Bobcats"


def strip_accents(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm(name):
    name = re.sub(r"[^a-z0-9 ]", " ", strip_accents(name).lower())
    return [_ALIAS.get(t, t) for t in name.split() if t and t not in _STOP]


def name_score(kalshi_name, odds_name):
    """1.0 if EVERY word of the (short) Kalshi name is the start of a different word in the
    sportsbook name, else 0.0. 'Los Angeles L' -> Lakers (not Clippers); 'Utah St.' -> Utah
    State Aggies (not Utah Utes). Strict on purpose: unmatched games are skipped, never guessed."""
    a, b = norm(kalshi_name), norm(odds_name)
    if not a or not b:
        return 0.0
    used = set()
    for tok in a:
        hit = next((i for i, w in enumerate(b) if i not in used and w.startswith(tok)), None)
        if hit is None:
            return 0.0
        used.add(hit)
    return 1.0


_MON = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def ticker_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m or m.group(2) not in _MON:
        return None
    try:
        return datetime(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3))).date()
    except ValueError:
        return None
