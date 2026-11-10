"""Kalshi 'scalar' settlements (YES pays e.g. $0.98, not $1/$0): settled at the real payout,
shown as 'Settled at 98¢', kept out of the W-L record; overdue picks are labeled."""
import json, os, shutil, sys, tempfile
from datetime import timedelta
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common, main, scanner, site_builder  # noqa: E402
from store import Store  # noqa: E402

work = tempfile.mkdtemp(); os.chdir(work)
cfg = common.load_config(); store = Store(cfg["db_path"])
now = common.now_utc()


def add(t, side, price, n, start):
    store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,contracts,prob,
               consensus,ev,n_books,book_std,staleness_min,research,start_ts,pick_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now.isoformat(), t, "E" + t, "KXNCAAMBGAME", f"Opp vs {t}", t, side, price, 0.1, n, 0.97, 0.97, 0,
             0, 0, None, "{}", start.isoformat(), "favorite"))


add("JAC", "yes", 0.97, 10, now - timedelta(hours=180))   # scalar 0.98 -> +$0.10 - fee
add("DUKE", "no", 0.04, 20, now - timedelta(hours=40))    # NO side on scalar 0.95 -> pays 0.05
add("WIN", "yes", 0.60, 10, now - timedelta(hours=3))     # normal yes
add("WAIT", "yes", 0.60, 10, now - timedelta(hours=20))   # not settled yet, overdue
M = {"JAC": {"ticker": "JAC", "status": "finalized", "result": "scalar", "settlement_value_dollars": "0.9800"},
     "DUKE": {"ticker": "DUKE", "status": "finalized", "result": "scalar", "settlement_value_dollars": "0.9500"},
     "WIN": {"ticker": "WIN", "status": "finalized", "result": "yes"},
     "WAIT": {"ticker": "WAIT", "status": "active", "result": ""}}
with mock.patch.object(scanner, "markets_by_tickers", lambda ts: [M[t] for t in ts if t in M]):
    assert main.settle_all(cfg, store) == 3
r = {x["team"]: x for x in store.q("SELECT * FROM trades")}
assert r["JAC"]["status"] == "settled" and abs(r["JAC"]["pnl"] - (10 * 0.98 - 10 * 0.97 - 0.1)) < 1e-9
assert abs(r["DUKE"]["pnl"] - (20 * 0.05 - 20 * 0.04 - 0.1)) < 1e-9
assert common.outcome(r["JAC"]) == "partial" and common.outcome(r["WIN"]) == "won"
assert r["WAIT"]["status"] == "open"
site_builder.render(cfg, store, [], None, False, [])
idx = open("docs/index.html").read(); h = open("docs/history.html").read()
assert ">1–0</a>" in idx, "partials stay out of the record"
assert "Settled at 98¢" in h and "Settled at 5¢" in h and "<b>1–0</b> record" in h
assert "2 picks Kalshi settled at a partial payout" in h
assert "Result overdue: Kalshi hasn&#x27;t settled this yet." in idx or "Result overdue: Kalshi hasn't settled this yet." in idx
print("SCALAR TESTS PASSED")
shutil.rmtree(work, ignore_errors=True)
