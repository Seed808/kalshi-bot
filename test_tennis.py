"""Tennis: tournament-key discovery, home/away fallback, accent-insensitive names,
full-name-only research (siblings), credit rationing by spend horizon."""
import json
import os
import sys
import tempfile
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["ODDS_API_KEY"] = "k"
import common, scanner, research  # noqa: E402
from store import Store  # noqa: E402

now = common.now_utc()
start = now + timedelta(hours=4)
end = (start + timedelta(hours=2.5)).isoformat()
far_end = (now + timedelta(hours=30)).isoformat()


def km(t, et, name, yb, ya, e=end):
    return {"ticker": t, "event_ticker": et, "yes_sub_title": name, "yes_bid_dollars": f"{yb:.4f}",
            "yes_ask_dollars": f"{ya:.4f}", "yes_ask_size_fp": "300.00", "yes_bid_size_fp": "300.00",
            "no_ask_dollars": f"{1 - yb:.4f}", "volume_24h_fp": "900.00", "expected_expiration_time": e,
            "last_price_dollars": f"{ya:.4f}", "previous_price_dollars": f"{ya:.4f}"}


ATP = [km("KXATPMATCH-X-CER", "KXATPMATCH-X", "Juan Manuel Cerundolo", 0.56, 0.57),
       km("KXATPMATCH-X-ALT", "KXATPMATCH-X", "Daniel Altmaier", 0.43, 0.44),
       km("KXATPMATCH-Y-MEN", "KXATPMATCH-Y", "Jakub Mensik", 0.85, 0.87),
       km("KXATPMATCH-Y-NEU", "KXATPMATCH-Y", "Lukas Neumayer", 0.13, 0.15)]
WTA_FAR = [km("KXWTAMATCH-Z-A", "KXWTAMATCH-Z", "Maya Joint", 0.26, 0.31, far_end),
           km("KXWTAMATCH-Z-B", "KXWTAMATCH-Z", "Sorana Cirstea", 0.66, 0.72, far_end)]
lu = (now - timedelta(minutes=4)).isoformat()


def bk(a, ap, b, bp):
    return {"last_update": lu, "markets": [{"key": "h2h", "last_update": lu, "outcomes": [
        {"name": a, "price": ap}, {"name": b, "price": bp}]}]}


BASEL = [{"id": "b1", "home_team": "Juan Manuel Cerundolo", "away_team": "Daniel Altmaier",
          "commence_time": start.isoformat(), "bookmakers": [bk("Juan Manuel Cerundolo", -130, "Daniel Altmaier", 110)] * 4}]
VIENNA = [{"id": "v1", "commence_time": start.isoformat(),   # no home/away fields -> fallback
           "bookmakers": [bk("Jakub Menšík", -600, "Lukas Neumayer", 450)] * 4}]
SPORTS = [{"key": "tennis_atp_swiss_indoors", "active": True, "has_outrights": False},
          {"key": "tennis_atp_vienna_open", "active": True, "has_outrights": False},
          {"key": "tennis_wta_pan_pacific_open", "active": True, "has_outrights": False},
          {"key": "tennis_atp_aus_open_winner", "active": True, "has_outrights": True},
          {"key": "basketball_nba", "active": True, "has_outrights": False}]
calls = {"odds": [], "sports": 0}


class R:
    def __init__(s, j): s._j, s.headers, s.status_code = j, {"x-requests-remaining": "480", "x-requests-last": "1"}, 200
    def json(s): return s._j


def fake(url, params=None, headers=None, retries=3):
    params = params or {}
    if url.endswith("/markets"):
        st = params.get("series_ticker")
        return R({"markets": ATP if st == "KXATPMATCH" else WTA_FAR if st == "KXWTAMATCH" else [], "cursor": ""})
    if "/series/" in url:
        return R({"series": {"fee_multiplier": 1}})
    if url.endswith("/v4/sports"):
        calls["sports"] += 1
        return R(SPORTS)
    for key, data in (("tennis_atp_swiss_indoors", BASEL), ("tennis_atp_vienna_open", VIENNA),
                      ("tennis_wta_pan_pacific_open", [])):
        if f"/sports/{key}/odds" in url:
            calls["odds"].append(key)
            return R(json.loads(json.dumps(data)))
    return R([])


cfg = common.load_config()
cfg["series"] = {"KXATPMATCH": "tennis_atp_*", "KXWTAMATCH": "tennis_wta_*"}
st = Store(cfg["db_path"])
with mock.patch.object(scanner, "http_get", fake):
    keys = scanner.resolve_sport_keys("tennis_atp_*", "k", st)
    assert keys == ["tennis_atp_swiss_indoors", "tennis_atp_vienna_open"], keys   # outrights excluded
    games, _ = scanner.sports_games(cfg, "k", st, verbose=True)
    scanner.resolve_sport_keys("tennis_atp_*", "k", st)
print("odds calls:", calls)
assert calls["sports"] == 1, "sports list should be cached"
assert sorted(calls["odds"]) == ["tennis_atp_swiss_indoors", "tennis_atp_vienna_open"], \
    "WTA has no match ending within 12h -> no credit spent"
names = sorted(g["matchup"] for g in games)
print("matched:", names)
assert len(games) == 2
mensik = [g for g in games if "Neumayer" in g["matchup"]][0]
assert mensik["mapping"]["KXATPMATCH-Y-MEN"] == "Jakub Menšík"     # accent-insensitive match
assert abs(mensik["oe"]["fair"]["Jakub Menšík"] - 0.82) < 0.03

# research: full names only for players
items = [{"src": "rss", "text": "Francisco Cerundolo ruled out with injury"},
         {"src": "rss", "text": "Juan Manuel Cerundolo cruises in practice"},
         {"src": "rss", "text": "Jakub Menšík questionable with wrist injury"}]
jm = research.score_team("Juan Manuel Cerundolo", items, nickname=False)
assert jm["mentions"] == 1 and jm["inj"] == 0, jm
assert research.score_team("Jakub Menšík", items, nickname=False)["inj"] == 1
assert research.score_team("Juan Manuel Cerundolo", items, nickname=True)["inj"] == 1  # why team mode is wrong here
print("TENNIS TESTS PASSED")
