"""Pick history: ESPN scores matched to settled picks (team sports, Kalshi short names, tennis groupings,
UFC), never a wrong game; history page newest first with outcomes; CSV export; Record links to it."""
import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common, scores, site_builder  # noqa: E402
from store import Store  # noqa: E402

work = tempfile.mkdtemp()
os.chdir(work)
cfg = common.load_config()
store = Store(cfg["db_path"])
D = datetime(2026, 10, 29, 1, 10, tzinfo=timezone.utc)   # Wed evening HST / Wed night Eastern


def add(series, matchup, team, start, result, side="yes", pnl=0.0, books=6, prob=0.7):
    store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,contracts,prob,
               consensus,ev,n_books,book_std,staleness_min,research,start_ts,pick_type,status,result,pnl,settled_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (D.isoformat(), f"M-{team}", f"E-{team}", series, matchup, team, side, 0.7, 0.15, 14, prob, prob,
             0.0, books, 0.01, 5, "{}", start.isoformat(), "favorite", "settled", result, pnl,
             (start + timedelta(hours=3)).isoformat()))


add("KXNBAGAME", "San Antonio Spurs @ Utah Jazz", "San Antonio Spurs", D, "yes", pnl=1.55)
add("KXNBAGAME", "Oklahoma City Thunder @ Dallas Mavericks", "Oklahoma City Thunder", D + timedelta(minutes=30), "no", pnl=-9.74)
add("KXMLSGAME", "Vancouver vs San Diego FC", "Vancouver", D + timedelta(hours=2), "yes", pnl=4.13, books=0)
add("KXWTAMATCH", "Naomi Osaka vs Mia Pohánková", "Naomi Osaka", D - timedelta(hours=2), "yes", side="no", pnl=-9.62)
add("KXUFCFIGHT", "Jacobe Smith vs Randy Brown", "Jacobe Smith", D - timedelta(days=1), "yes", pnl=3.1)
add("KXNBAGAME", "Los Angeles Lakers @ Golden State Warriors", "Los Angeles Lakers", D - timedelta(days=2), "yes", pnl=4.0)
store.x("INSERT INTO trades(ts,series,matchup,team,side,price,fee,contracts,prob,n_books,start_ts,status,void_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (D.isoformat(), "KXNBAGAME", "A @ B", "A", "yes", 0.5, 0.1, 10, 0.5, 6,
                                              D.isoformat(), "void", "trimmed"))
store.x("INSERT INTO trades(ts,series,matchup,team,side,price,fee,contracts,prob,n_books,start_ts,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (D.isoformat(), "KXNBAGAME", "C @ E", "C", "yes", 0.5, 0.1, 10, 0.6, 6,
                                            (D + timedelta(days=1)).isoformat(), "open"))

FINAL = {"type": {"completed": True, "shortDetail": "Final"}}


def team(display, short, location, name, score, winner):
    return {"team": {"displayName": display, "shortDisplayName": short, "location": location, "name": name},
            "score": score, "winner": winner}


BOARDS = {
    "basketball/nba": {"events": [
        {"status": FINAL, "competitions": [{"status": FINAL, "competitors": [
            team("Utah Jazz", "Jazz", "Utah", "Jazz", "104", False),
            team("San Antonio Spurs", "Spurs", "San Antonio", "Spurs", "118", True)]}]},
        {"status": FINAL, "competitions": [{"status": FINAL, "competitors": [
            team("Dallas Mavericks", "Mavericks", "Dallas", "Mavericks", "121", True),
            team("Oklahoma City Thunder", "Thunder", "Oklahoma City", "Thunder", "117", False)]}]},
        # Clippers game on the Lakers' date: must NOT be used for the Lakers pick
        {"status": FINAL, "competitions": [{"status": FINAL, "competitors": [
            team("LA Clippers", "Clippers", "Los Angeles", "Clippers", "99", False),
            team("Golden State Warriors", "Warriors", "Golden State", "Warriors", "101", True)]}]}]},
    "soccer/usa.1": {"events": [{"status": {"type": {"completed": True, "shortDetail": "FT"}}, "competitions": [{
        "competitors": [team("Vancouver Whitecaps", "Vancouver", "Vancouver", "Whitecaps", "2", True),
                        team("San Diego FC", "San Diego", "San Diego", "FC", "1", False)]}]}]},
    "tennis/wta": {"events": [{"name": "Japan Open", "groupings": [{"grouping": {"slug": "womens-singles"},
        "competitions": [{"status": FINAL, "competitors": [
            {"athlete": {"displayName": "Naomi Osaka", "shortName": "N. Osaka"},
             "linescores": [{"value": 4}, {"value": 6}, {"value": 3}], "winner": False},
            {"athlete": {"displayName": "Mia Pohankova", "shortName": "M. Pohankova"},
             "linescores": [{"value": 6}, {"value": 2}, {"value": 6}], "winner": True}]}]}]}]},
    "mma/ufc": {"events": [{"competitions": [{"status": FINAL, "competitors": [
        {"athlete": {"displayName": "Randy Brown", "shortName": "R. Brown"}, "winner": False},
        {"athlete": {"displayName": "Jacobe Smith", "shortName": "J. Smith"}, "winner": True}]}]}]},
}
calls = []


class R:
    def __init__(self, j):
        self._j = j

    def json(self):
        return self._j


def fake_get(url, params=None, headers=None, retries=3):
    path = url.split("/sports/")[1].rsplit("/scoreboard", 1)[0]
    calls.append((path, params.get("dates")))
    return R(BOARDS.get(path, {"events": []}))


with mock.patch.object(scores, "http_get", fake_get):
    found = scores.backfill(store, verbose=True)
got = {r["team"]: r["score_text"] for r in store.q("SELECT team, score_text FROM trades WHERE status='settled'")}
print(json.dumps(got, indent=1, ensure_ascii=False))
assert got["San Antonio Spurs"] == "Spurs 118, Jazz 104 (Final)"
assert got["Oklahoma City Thunder"] == "Thunder 117, Mavericks 121 (Final)"
assert got["Vancouver"] == "Vancouver 2, San Diego 1 (FT)"
assert got["Naomi Osaka"] == "N. Osaka 4-6, 6-2, 3-6 (Final)"
assert got["Jacobe Smith"] == "J. Smith won (Final)"
assert got["Los Angeles Lakers"] is None, "must not take the Clippers' score"
assert found == 5
assert ("basketball/nba", "20281028") not in calls and ("basketball/nba", "20261028") in calls
# winner check: a Kalshi win paired with an ESPN loss must not get that score
store.x("UPDATE trades SET score_text=NULL, score_tries=0, result='no' WHERE team='San Antonio Spurs'")
with mock.patch.object(scores, "http_get", fake_get):
    scores.backfill(store)
assert store.q("SELECT score_text FROM trades WHERE team='San Antonio Spurs'")[0][0] is None
store.x("UPDATE trades SET result='yes', score_tries=0 WHERE team='San Antonio Spurs'")
with mock.patch.object(scores, "http_get", fake_get):
    scores.backfill(store)
assert store.q("SELECT score_text FROM trades WHERE team='San Antonio Spurs'")[0][0] == "Spurs 118, Jazz 104 (Final)"
tries = store.q("SELECT score_tries FROM trades WHERE team='Los Angeles Lakers'")[0][0]
assert tries == 3
with mock.patch.object(scores, "http_get", fake_get):   # found ones aren't fetched again
    calls.clear(); scores.backfill(store)
assert all(c[0] == "basketball/nba" for c in calls)

# ---- pages
site_builder.render(cfg, store, [], None, False, [])
idx = open("docs/index.html").read()
assert 'href="history.html"' in idx and "See pick history" in idx
h = open("docs/history.html").read()
body = h.split("<tbody>")[1].split("</tbody>")[0]
order = [t for t in ("Vancouver", "Oklahoma City Thunder", "San Antonio Spurs", "Naomi Osaka", "Jacobe Smith",
                     "Los Angeles Lakers") if t in body]
assert [body.index(f"<b>{t}</b>") for t in order] == sorted(body.index(f"<b>{t}</b>") for t in order), "newest first"
assert body.count("<tr") == 6, "settled only: no voided, no open picks"
assert body.count("Pick won") == 4 and body.count("Pick lost") == 2
assert "Spurs 118, Jazz 104 (Final)" in body and "not available" in body
assert '<b>4–2</b> record' in h and 'href="index.html"' in h and 'href="history.csv"' in h
rows = list(csv.reader(open("docs/history.csv", encoding="utf-8")))
assert rows[0][0] == "Date (HST)" and len(rows) == 7 and rows[1][2] == "MLS" and rows[1][9] == "Pick won"
print("HISTORY TESTS PASSED")
shutil.rmtree(work, ignore_errors=True)
