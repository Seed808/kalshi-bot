"""Slate rules: 55% floor unless a clear value underdog, highest win chance first with ~25% of slots
held for value underdogs, 15 open / 15 a day, trimming an oversized slate, site shows only the slate."""
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common, picks, risk, site_builder  # noqa: E402
from store import Store  # noqa: E402

work = tempfile.mkdtemp()
os.chdir(work)
cfg = common.load_config()
assert (cfg["max_open_trades"], cfg["max_trades_per_day"], cfg["pick_min_win_chance"]) == (15, 15, 0.55)

# ---- the floor
Q = lambda prob, cost, books, dog: picks.qualifies(prob, cost, books, dog, cfg)
assert Q(0.60, 0.62, True, False) is None
assert "under 55%" in Q(0.52, 0.53, True, False)
assert Q(0.40, 0.35, True, True) is None                         # +14% per dollar: clear value underdog
assert "isn't a clear value bet" in Q(0.40, 0.39, True, True)    # +2.6%: not enough
assert "isn't a clear value bet" in Q(0.40, 0.35, False, True)   # Kalshi-only: no outside price to judge value
assert Q(0.58, 0.70, False, True) is None                        # underdog pick that's still >= 55% is fine

# ---- order: favorites by win chance, 4 of 15 slots held for value underdogs (best value first)
favs = [{"prob": 0.56 + i / 100, "value": -0.02, "is_dog": False, "from_books": True, "id": f"F{i}"} for i in range(20)]
dogs = [{"prob": 0.30 + i / 100, "value": 0.08 + i / 100, "is_dog": True, "from_books": True, "id": f"D{i}"} for i in range(6)]
order = picks.slate_order(favs + dogs, room=15, open_dogs=0, cfg=cfg)
first15 = [x["id"] for x in order[:15]]
assert first15[:11] == [f"F{i}" for i in range(19, 8, -1)], first15     # 11 highest win chances
assert first15[11:] == ["D5", "D4", "D3", "D2"], first15                  # 4 best-value underdogs
assert [x["id"] for x in picks.slate_order(favs + dogs, 15, 4, cfg)[:15]] == [f"F{i}" for i in range(19, 4, -1)]
assert [x["id"] for x in picks.slate_order(favs[:3] + dogs, 15, 0, cfg)][:7] == ["F2", "F1", "F0", "D5", "D4", "D3", "D2"]

# ---- trimming today's 47 open picks down to the best 15
store = Store(cfg["db_path"])
now = common.now_utc()
random.seed(3)


def add(team, prob, price, kind, books, start, n=10, fee=0.15):
    store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,contracts,prob,
               consensus,ev,n_books,book_std,staleness_min,research,start_ts,pick_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now.isoformat(), f"M-{team}", f"E-{team}", "KXNBAGAME", f"{team} @ Opp {team}", team, "yes", price,
             fee, n, prob, prob, prob - price, books, 0.01, 5, "{}", start.isoformat(), kind))


later = now + timedelta(hours=6)
for i in range(36):                                  # favorites 50-85%, some Kalshi-only
    p = 0.50 + i / 100
    add(f"Fav{i}", p, round(p + 0.01, 2), "favorite", 0 if i % 4 == 0 else 6, later)
for i in range(8):                                   # coin underdogs: 3 clear value, 5 not
    p = 0.35
    add(f"Dog{i}", p, 0.30 if i < 3 else 0.35, "upset", 6, later)
add("Inj", 0.30, 0.31, "injury", 6, later)
add("Coinflip", 0.505, 0.51, "favorite", 0, later)   # the Berrettini case: 50.5%, Kalshi-only
add("Started", 0.52, 0.53, "favorite", 6, now - timedelta(minutes=30))   # under way: never voided
assert store.open_count() == 47

voided = picks.trim_slate(store, cfg, now, common.parse_ts)
opens = store.open_trades()
names = {r["team"] for r in opens}
print("kept:", sorted(names, key=lambda t: t))
print("void reasons:", sorted({w for _r, w in voided}))
assert len(opens) == 15 and len(voided) == 32
assert "Started" in names                            # games already under way are left alone
assert "Coinflip" not in names and "Inj" not in names
assert {"Dog0", "Dog1", "Dog2"} <= names and not any(f"Dog{i}" in names for i in range(3, 8))
fav_kept = sorted((r["prob"] for r in opens if r["team"].startswith("Fav")), reverse=True)
assert len(fav_kept) == 11 and abs(fav_kept[-1] - 0.75) < 1e-9, fav_kept   # the 11 highest win chances (0.85..0.75)
assert all(r["prob"] >= 0.55 or r["team"].startswith("Dog") or r["team"] == "Started" for r in opens)
assert picks.trim_slate(store, cfg, now, common.parse_ts) == []   # idempotent

# voided picks don't count toward today's limit, bankroll or record
assert store.trades_today() == 15
bankroll, exposure = store.bankroll(cfg)
assert bankroll == cfg["bankroll"] and abs(exposure - sum(r["price"] * r["contracts"] + r["fee"] for r in opens)) < 1e-9
n, why = risk.check_pick({"event_ticker": "NEW", "cost": 0.6}, cfg, store)
assert n == 0 and "daily pick limit (15)" in why, why

# ---- site: only the active slate
site_builder.render(cfg, store, [], None, False, [])
html = open("docs/index.html").read()
assert html.count('<article class="play') == 15
for gone in ("Every game checked", "Games skipped", "Unusual Kalshi markets", "<h2>Record</h2>"):
    assert gone not in html, gone
priced = sum(1 for r in store.open_trades() if r["n_books"])
assert f"Best Bet #1 of {priced}" in html and '<p class="do">#15 ' in html
assert html.count("not ranked (no sportsbook price to compare)") == 15 - priced
print("SLATE TESTS PASSED")
shutil.rmtree(work, ignore_errors=True)
