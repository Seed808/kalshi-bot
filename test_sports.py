"""Multi-sport pick mode with mocked data:
- soccer (3 outcomes incl. tie): sportsbook 3-way odds -> picks a TEAM to win, YES only
- soccer with no sportsbook feed (MLS key not in season) -> Kalshi-price fallback, tie excluded
- college hockey (no Odds API key at all) -> Kalshi-price fallback; game already under way is not picked
- UFC (individual names) and men's college basketball ("St." vs "St") via sportsbooks
- every pick is labeled with its sport in Telegram and on the site; no credits spent on MLS/hockey
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
for k in ("TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC", "X_BEARER_TOKEN", "REDDIT_CLIENT_ID"):
    os.environ.pop(k, None)

now = common.now_utc()
t0 = now + timedelta(hours=10)
lu = (now - timedelta(minutes=20)).isoformat()


def km(t, et, name, yb, ya, end):
    return {"ticker": t, "event_ticker": et, "yes_sub_title": name, "yes_bid_dollars": f"{yb:.4f}",
            "yes_ask_dollars": f"{ya:.4f}", "yes_ask_size_fp": "300.00", "yes_bid_size_fp": "300.00",
            "no_ask_dollars": f"{1 - yb:.4f}", "volume_24h_fp": "40.00",
            "expected_expiration_time": end.isoformat(),
            "last_price_dollars": f"{ya:.4f}", "previous_price_dollars": f"{ya:.4f}"}


def bk(outcomes):
    return {"last_update": lu, "markets": [{"key": "h2h", "last_update": lu,
                                            "outcomes": [{"name": n, "price": p} for n, p in outcomes]}]}


EPL, MLS, HOC, HOC_LIVE, UFC, CBB = ("KXEPLGAME-X-BRIBRE", "KXMLSGAME-X-ATXPOR", "KXNCAAHGAME-X-UMLCLG",
                                     "KXNCAAHGAME-X-STTMIA", "KXUFCFIGHT-X-BROSMI", "KXNCAAMBGAME-X-CCSUTEM")
SERIES_MARKETS = {
    "KXEPLGAME": [km(EPL + "-BRI", EPL, "Brighton", 0.44, 0.46, t0 + timedelta(hours=2)),
                  km(EPL + "-BRE", EPL, "Brentford", 0.28, 0.30, t0 + timedelta(hours=2)),
                  km(EPL + "-TIE", EPL, "Tie", 0.25, 0.27, t0 + timedelta(hours=2))],
    "KXMLSGAME": [km(MLS + "-ATX", MLS, "Austin", 0.44, 0.47, t0 + timedelta(hours=2)),
                  km(MLS + "-POR", MLS, "Portland", 0.30, 0.34, t0 + timedelta(hours=2)),
                  km(MLS + "-TIE", MLS, "Tie", 0.22, 0.26, t0 + timedelta(hours=2))],
    "KXNCAAHGAME": [km(HOC + "-UML", HOC, "Massachusetts Lowell", 0.55, 0.60, t0 + timedelta(hours=3)),
                    km(HOC + "-CLG", HOC, "Colgate", 0.38, 0.43, t0 + timedelta(hours=3)),
                    # ends in 1h: started already by the 3h estimate -> must not be picked
                    km(HOC_LIVE + "-STT", HOC_LIVE, "St. Thomas", 0.80, 0.82, now + timedelta(hours=1)),
                    km(HOC_LIVE + "-MIA", HOC_LIVE, "Miami (OH)", 0.17, 0.19, now + timedelta(hours=1))],
    "KXUFCFIGHT": [km(UFC + "-SMI", UFC, "Jacobe Smith", 0.69, 0.72, t0 + timedelta(hours=5)),
                   km(UFC + "-BRO", UFC, "Randy Brown", 0.28, 0.29, t0 + timedelta(hours=5))],
    "KXNCAAMBGAME": [km(CBB + "-TEM", CBB, "Temple", 0.91, 0.95, t0 + timedelta(hours=2.5)),
                     km(CBB + "-CCSU", CBB, "Central Connecticut St.", 0.04, 0.08, t0 + timedelta(hours=2.5))],
}
ODDS = {
    "soccer_epl": [{"id": "e1", "home_team": "Brighton and Hove Albion", "away_team": "Brentford",
                    "commence_time": t0.isoformat(),
                    "bookmakers": [bk([("Brighton and Hove Albion", 120), ("Brentford", 230), ("Draw", 250)])] * 3}],
    "mma_mixed_martial_arts": [{"id": "m1", "home_team": "Jacobe Smith", "away_team": "Randy Brown",
                                "commence_time": t0.isoformat(),
                                "bookmakers": [bk([("Jacobe Smith", -250), ("Randy Brown", 200)])] * 3}],
    "basketball_ncaab": [{"id": "b1", "home_team": "Temple Owls", "away_team": "Central Connecticut St Blue Devils",
                          "commence_time": t0.isoformat(),
                          "bookmakers": [bk([("Temple Owls", -1400), ("Central Connecticut St Blue Devils", 800)])] * 3}],
}
SPORTS = [{"key": k, "active": True, "has_outrights": False} for k in ODDS]   # MLS not in season
calls = []


class R:
    def __init__(self, j):
        self._j, self.headers, self.status_code = j, {"x-requests-remaining": "400", "x-requests-last": "1"}, 200

    def json(self):
        return self._j


def fake_get(url, params=None, headers=None, retries=3):
    params = params or {}
    if url.endswith("/markets"):
        st = params.get("series_ticker")
        allm = [m for ms in SERIES_MARKETS.values() for m in ms]
        return R({"markets": SERIES_MARKETS.get(st, []) if st else allm, "cursor": ""})
    if "/series/" in url:
        return R({"series": {"fee_multiplier": 1}})
    if "/events/" in url:
        return R({"event": {}})
    if url.endswith("/v4/sports"):
        return R(SPORTS)
    for key, data in ODDS.items():
        if f"/sports/{key}/odds" in url:
            calls.append(key)
            return R(json.loads(json.dumps(data)))
    if "/odds" in url:
        calls.append(url)
    return R([])


alerts = []
patches = [mock.patch.object(scanner, "http_get", fake_get), mock.patch.object(main, "http_get", fake_get),
           mock.patch.object(research, "fetch_rss", lambda urls, lb: []),
           mock.patch.object(research, "fetch_reddit", lambda subs, lb: []),
           mock.patch.object(research, "fetch_x", lambda team, cfg: []),
           mock.patch.object(main, "alert", lambda t: alerts.append(t))]
for p in patches:
    p.start()

assert scanner.resolve_sport_keys(None, "k", None) == []
cfg = common.load_config()
store = Store(cfg["db_path"])
main.cycle("config.json", store, paper=True, verbose=True)

T = {t["series"]: t for t in store.open_trades()}
for s_, t in T.items():
    print(f'{s_:<14} {t["team"]:<26} {t["pick_type"]:<9} {t["prob"]:.3f} side={t["side"]} '
          f'mkt={t["market_ticker"]} books={t["n_books"]}')
assert set(T) == {"KXEPLGAME", "KXMLSGAME", "KXNCAAHGAME", "KXUFCFIGHT", "KXNCAAMBGAME"}, set(T)
assert sorted(calls) == ["basketball_ncaab", "mma_mixed_martial_arts", "soccer_epl"], calls  # no MLS/hockey credits


def expect(et, fav, dog):
    return (dog, "upset") if picks.upset_coin(et, cfg["pick_upset_rate"]) else (fav, "favorite")


# soccer via sportsbooks: a TEAM (never the tie), YES on that team's own market
e = T["KXEPLGAME"]
assert (e["team"], e["pick_type"]) == expect(EPL, "Brighton and Hove Albion", "Brentford"), dict(e)
assert e["side"] == "yes" and e["market_ticker"] in (EPL + "-BRI", EPL + "-BRE") and e["n_books"] == 3
assert e["prob"] < 0.5   # 3-way: favorite under 50% because the tie takes a share
# soccer via Kalshi prices (MLS feed not in season): tie excluded, YES only
m = T["KXMLSGAME"]
assert (m["team"], m["pick_type"]) == expect(MLS, "Austin", "Portland") and m["side"] == "yes" and m["n_books"] == 0
assert not m["market_ticker"].endswith("-TIE")
# college hockey via Kalshi prices; the game under way is not picked
h = T["KXNCAAHGAME"]
assert h["event_ticker"] == HOC and h["n_books"] == 0
assert (h["team"], h["pick_type"]) == expect(HOC, "Massachusetts Lowell", "Colgate")
assert not store.q("SELECT 1 FROM trades WHERE event_ticker=?", (HOC_LIVE,))
# UFC and men's college basketball via sportsbooks
assert T["KXUFCFIGHT"]["n_books"] == 3 and T["KXNCAAMBGAME"]["n_books"] == 3
assert (T["KXNCAAMBGAME"]["team"], T["KXNCAAMBGAME"]["pick_type"]) == expect(
    CBB, "Temple Owls", "Central Connecticut St Blue Devils")

msg = alerts[0]
print(msg)
for lab in ("[Premier League]", "[MLS]", "[NCAA Hockey]", "[UFC]", "[NCAA Men's Basketball]"):
    assert lab in msg, lab
assert "Kalshi prices give" in msg and "Sportsbooks give" in msg
html = open("docs/index.html").read()
for lab in ("Premier League", "MLS", "NCAA Hockey", "UFC", "NCAA Men&#x27;s Basketball"):
    assert f'<p class="sport">{lab}</p>' in html, lab
assert "no sportsbook line: Kalshi prices" in html

for p in patches:
    p.stop()
shutil.rmtree(work, ignore_errors=True)
print("MULTI-SPORT TESTS PASSED")
