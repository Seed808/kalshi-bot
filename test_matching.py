"""Regression: back-to-back games between the same teams must never cross-match."""
import os, sys
from datetime import timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, scanner

now = common.now_utc(); g1 = now + timedelta(hours=10); g2 = g1 + timedelta(days=1)
def mk(t, et, n, end): return {"ticker": t, "event_ticker": et, "yes_sub_title": n,
                               "expected_expiration_time": end.isoformat() if end else None}
k26 = [mk("A1", "E26", "Milwaukee", g1 + timedelta(hours=3.5)), mk("B1", "E26", "Tampa Bay", g1 + timedelta(hours=3.5))]
k27 = [mk("A2", "E27", "Milwaukee", g2 + timedelta(hours=3.5)), mk("B2", "E27", "Tampa Bay", g2 + timedelta(hours=3.5))]
o26 = {"id": "o26", "home_team": "Tampa Bay Rays", "away_team": "Milwaukee Brewers", "commence_time": g1.isoformat()}
o27 = dict(o26, id="o27", commence_time=g2.isoformat())
assert scanner.match_event("E26", k26, [o26], 0.5)[0]["id"] == "o26"
assert scanner.match_event("E27", k27, [o26], 0.5)[0] is None
assert scanner.match_event("E26", k26, [o26, o27], 0.5)[0]["id"] == "o26"
assert scanner.match_event("E27", k27, [o26, o27], 0.5)[0]["id"] == "o27"
assert scanner.match_event("E26", [mk("A1", "E26", "Milwaukee", None), mk("B1", "E26", "Tampa Bay", None)], [o26], 0.5)[0] is None
print("MATCHING TESTS PASSED")
