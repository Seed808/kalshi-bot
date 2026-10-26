"""Free-plan guard: odds credits never exceed the daily allowance; X stays off."""
import os, sys, tempfile, json
from datetime import timedelta
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
import common, scanner, research
from store import Store

cfg = common.load_config(); cfg["odds_cache_min"] = 0  # force a fetch attempt every call
st = Store(cfg["db_path"])
calls = {"n": 0}
now = common.now_utc()
ev = [{"home_team": "A", "away_team": "B", "commence_time": (now + timedelta(hours=5)).isoformat(), "bookmakers": []}]
class R:
    def __init__(s): s.headers = {"x-requests-remaining": str(500 - calls["n"]), "x-requests-last": "1"}
    def json(s): return ev
def fake(url, params=None, headers=None, retries=3):
    calls["n"] += 1; return R()
with mock.patch.object(scanner, "http_get", fake):
    for _ in range(200):  # 200 attempts in one day
        scanner.odds_consensus("basketball_nba", cfg, "k", st)
month, today = st.odds_credits_used(common.now_utc())
nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
days_left = (nxt.date() - now.date()).days
print(f"attempts 200 -> real calls {calls['n']}, credits today {today:.0f}, "
      f"allowance ~{min(450/days_left,15):.1f}/day ({days_left} days left in month)")
assert calls["n"] <= min(450 / days_left, 15) + 1
ok, why = scanner.odds_budget_ok(cfg, st); assert not ok; print("then blocked:", why)
# reserve: API says only 20 left -> no calls at all
st2 = Store(os.path.join(tempfile.mkdtemp(), "b.db")); st2.set_meta("odds_remaining", 20)
assert not scanner.odds_budget_ok(cfg, st2)[0]; print("reserve guard OK:", scanner.odds_budget_ok(cfg, st2)[1])
# X off by default even if a token exists
os.environ["X_BEARER_TOKEN"] = "set"
assert cfg["x_enabled"] is False and cfg["x_monthly_read_cap"] == 0
with mock.patch.object(research, "fetch_rss", lambda u, l: []), mock.patch.object(research, "fetch_reddit", lambda s, l: []), \
     mock.patch.object(research, "fetch_x", side_effect=AssertionError("X called!")) if False else mock.patch.object(research, "fetch_x", lambda t, c: (_ for _ in ()).throw(AssertionError("X was called"))):
    g = {"series": "KXNBAGAME", "mapping": {"a": "Team A", "b": "Team B"}}
    research.research_games([g], cfg, st)
print("X never called with default config")
print("BUDGET TESTS PASSED")
