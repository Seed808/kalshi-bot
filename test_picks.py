"""Pick mode, end to end with mocked Kalshi / Odds API / news:
- every matched game gets a pick, even with 90-minute-old odds and no edge
- the pick is the favorite unless the seeded upset coin or injury news says underdog
- fixed $10 paper stake, cheapest Kalshi route, no duplicates on re-run
- open-pick cap still applies; unmatched games are listed as skipped with a reason
- results are reported in one message; no self-tuning in pick mode
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common, scanner, research, picks, main  # noqa: E402
from store import Store  # noqa: E402

work = tempfile.mkdtemp()
os.chdir(work)
os.environ["ODDS_API_KEY"] = "test"
for k in ("X_BEARER_TOKEN", "REDDIT_CLIENT_ID", "TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC"):
    os.environ.pop(k, None)

# ---- the coin: seeded, stable, about 25%
rate = sum(picks.upset_coin(f"KXNBAGAME-T{i}", 0.25) for i in range(4000)) / 4000
assert 0.22 < rate < 0.28, rate
assert picks.upset_coin("X-1", 0.25) == picks.upset_coin("X-1", 0.25)

now = common.now_utc()
start = now + timedelta(hours=8)
d = (start - timedelta(hours=5)).strftime("%y%b%d").upper()
old = (now - timedelta(minutes=90)).isoformat()   # 90-minute-old odds: edge mode would refuse these


def book(h, hp, a, ap):
    return {"last_update": old, "markets": [{"key": "h2h", "last_update": old, "outcomes": [
        {"name": h, "price": hp}, {"name": a, "price": ap}]}]}


GAMES = [  # (event suffix, kalshi names (away, home), odds names (away, home), home price, away price)
    ("LALBOS", ("Los Angeles L", "Boston"), ("Los Angeles Lakers", "Boston Celtics"), -200, 170),
    ("NYKMIA", ("New York", "Miami"), ("New York Knicks", "Miami Heat"), 120, -140),
    ("DENPHX", ("Denver", "Phoenix"), ("Denver Nuggets", "Phoenix Suns"), 150, -175),
    ("GSWSAC", ("Golden State", "Sacramento"), ("Golden State Warriors", "Sacramento Kings"), -110, -110),
    ("ORLCHI", ("Orlando", "Chicago"), ("Orlando Magic", "Chicago Bulls"), 130, -150),
]
ODDS, MARKETS = [], {}
end = (start + timedelta(hours=2.5)).isoformat()


def mk(t, et, name, yb, ya):
    return {"ticker": t, "event_ticker": et, "yes_sub_title": name, "yes_bid_dollars": f"{yb:.4f}",
            "yes_ask_dollars": f"{ya:.4f}", "yes_ask_size_fp": "500.00", "yes_bid_size_fp": "400.00",
            "no_ask_dollars": f"{1 - yb:.4f}", "volume_24h_fp": "50.00", "expected_expiration_time": end,
            "last_price_dollars": f"{ya:.4f}", "previous_price_dollars": f"{ya:.4f}"}


for suf, (ka, kh), (oa, oh), hp, ap in GAMES:
    et = f"KXNBAGAME-{d}{suf}"
    ph, _ = common.devig_two_way(common.american_to_decimal(hp), common.american_to_decimal(ap))
    # Kalshi priced right at consensus (no edge either side)
    MARKETS[et + "-H"] = mk(et + "-H", et, kh, round(ph, 2) - 0.01, round(ph, 2) + 0.01)
    MARKETS[et + "-A"] = mk(et + "-A", et, ka, round(1 - ph, 2) - 0.01, round(1 - ph, 2) + 0.01)
    ODDS.append({"id": suf, "home_team": oh, "away_team": oa, "commence_time": start.isoformat(),
                 "bookmakers": [book(oh, hp, oa, ap)] * 3})
# a Kalshi game with no sportsbook line
UNM = f"KXNBAGAME-{d}PORMEM"
MARKETS[UNM + "-P"] = mk(UNM + "-P", UNM, "Portland", 0.10, 0.90)   # thin: Kalshi fallback can't use it
MARKETS[UNM + "-M"] = mk(UNM + "-M", UNM, "Memphis", 0.10, 0.90)
RESULTS = {}


class R:
    def __init__(self, j, h=None):
        self._j, self.headers, self.status_code = j, h or {}, 200

    def json(self):
        return self._j


def fake_get(url, params=None, headers=None, retries=3):
    params = params or {}
    if url.endswith("/markets"):
        if "tickers" in params:
            return R({"markets": [dict(MARKETS[t], result=RESULTS.get(t, ""))
                                  for t in params["tickers"].split(",") if t in MARKETS], "cursor": ""})
        if params.get("series_ticker") == "KXNBAGAME":
            return R({"markets": list(MARKETS.values()), "cursor": ""})
        return R({"markets": [] if params.get("series_ticker") else list(MARKETS.values()), "cursor": ""})
    if "/series/" in url:
        return R({"series": {"fee_multiplier": 1}})
    if "/events/" in url:
        return R({"event": {}})
    if "basketball_nba/odds" in url:
        return R(json.loads(json.dumps(ODDS)), {"x-requests-remaining": "450", "x-requests-last": "1"})
    return R([])


NEWS = [{"src": "rss", "text": "Knicks star ruled out Tuesday with injury"},
        {"src": "rss", "text": "New York Knicks guard questionable, injured ankle"}]
alerts = []
patches = [mock.patch.object(scanner, "http_get", fake_get), mock.patch.object(main, "http_get", fake_get),
           mock.patch.object(research, "fetch_rss", lambda urls, lb: NEWS if urls else []),
           mock.patch.object(research, "fetch_reddit", lambda subs, lb: []),
           mock.patch.object(research, "fetch_x", lambda team, cfg: []),
           mock.patch.object(main, "alert", lambda t: alerts.append(t))]
for p in patches:
    p.start()

cfg = common.load_config()
assert cfg["mode"] == "picks"
store = Store(cfg["db_path"])
main.cycle("config.json", store, paper=True, verbose=True)

trades = {t["event_ticker"].split(d)[1]: t for t in store.open_trades()}
print("picks:", {k: (t["team"], t["pick_type"], round(t["prob"], 3), t["contracts"]) for k, t in trades.items()})
assert len(trades) == len(GAMES), "every matched game should get a pick despite stale odds and no edge"
for suf, (ka, kh), (oa, oh), hp, ap in GAMES:
    t = trades[suf]
    ph, _ = common.devig_two_way(common.american_to_decimal(hp), common.american_to_decimal(ap))
    fav, dog = (oh, oa) if ph >= 0.5 else (oa, oh)
    if suf == "NYKMIA":
        assert (t["team"], t["pick_type"]) == ("Miami Heat", "injury"), dict(t)   # Knicks injury news
    elif picks.upset_coin(f"KXNBAGAME-{d}{suf}", cfg["pick_upset_rate"]):
        assert (t["team"], t["pick_type"]) == (dog, "upset"), dict(t)
    else:
        assert (t["team"], t["pick_type"]) == (fav, "favorite"), dict(t)
    cost = t["price"] + common.fee_per_contract(t["price"])
    assert t["contracts"] == int(10 // cost), (t["contracts"], cost)
assert len(alerts) == 1 and alerts[0].startswith(f"NEW PICKS ({len(GAMES)})"), alerts
assert "[NBA] " in alerts[0]
print(alerts[0][:400])

board = json.load(open("docs/board.json"))
sk = board["skipped"]
assert any("Portland" in x["names"] and "too thin" in x["reason"] for x in sk), sk
html = open("docs/index.html").read()
assert f"{len(GAMES)} open picks" in html and "Games skipped" in html and "Portland vs Memphis" in html
assert '<p class="sport">NBA</p>' in html

# ---- re-run: no duplicates, board shows the earlier picks
main.cycle("config.json", store, paper=True, verbose=False)
assert store.open_count() == len(GAMES) and len(alerts) == 1
assert all(b["pick"]["status"] == "open" for b in json.load(open("docs/board.json"))["board"])

# ---- settle every game: favorites (home) win; one results message; no self-tuning
store.x("UPDATE trades SET start_ts=?", ((now - timedelta(hours=4)).isoformat(),))
for t in list(MARKETS):
    RESULTS[t] = "yes" if t.endswith("-H") else "no"
main.cycle("config.json", store, paper=True, verbose=False)
res_msgs = [a for a in alerts if a.startswith("RESULTS")]
assert len(res_msgs) == 1 and res_msgs[0].count("\n") >= len(GAMES), res_msgs
print(res_msgs[0])
assert not os.path.exists("learned_overrides.json"), "pick mode must not self-tune"
assert len(store.settled_trades()) == len(GAMES)

# ---- caps still apply: fresh db with max 2 open picks
open("config.json", "w").write(json.dumps({"max_open_trades": 2, "db_path": "capped.db"}))
RESULTS.clear()
store2 = Store("capped.db")
main.cycle("config.json", store2, paper=True, verbose=False)
assert store2.open_count() == 2
blocked = [b for b in json.load(open("docs/board.json"))["board"] if b["pick"]["status"] == "blocked"]
assert len(blocked) == len(GAMES) - 2 and "open-pick limit (2)" in blocked[0]["why"], blocked
# the two that got in are the strongest picks (highest win chance), not the soonest games
all_probs = sorted((b["pick"]["prob"] for b in json.load(open("docs/board.json"))["board"]), reverse=True)
assert sorted((t["prob"] for t in store2.open_trades()), reverse=True) == all_probs[:2]

# long alerts are split into several messages, not cut off
sent = []
for p in patches:
    p.stop()
with mock.patch.object(main, "_send", lambda t: sent.append(t)):
    main.alert("NEW PICKS (40)\n\n" + "\n\n".join("x" * 230 for _ in range(40)))
assert len(sent) == 3 and sent[0].startswith("(1/3) NEW PICKS") and all(len(t) <= 3900 for t in sent), \
    [len(t) for t in sent]
patches = []

for p in patches:
    p.stop()
shutil.rmtree(work, ignore_errors=True)
print("PICK MODE TESTS PASSED")
