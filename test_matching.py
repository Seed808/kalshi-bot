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

# ---- real names seen in the live log
from common import name_score as ns
assert ns("Texas St.", "Texas State Bobcats") == 1.0
assert ns("Utah St.", "Utah State Aggies") == 1.0
assert ns("Utah St.", "Utah Utes") == 0.0
assert ns("Los Angeles L", "Los Angeles Lakers") == 1.0
assert ns("Los Angeles L", "Los Angeles Clippers") == 0.0
assert ns("Los Angeles C", "Los Angeles Clippers") == 1.0
assert ns("New York R", "New York Rangers") == 1.0
assert ns("New York R", "New York Islanders") == 0.0
assert ns("Texas St.", "Texas Longhorns") == 0.0
assert ns("UNLV", "UNLV Rebels") == 1.0
assert ns("Norfolk St.", "Norfolk State Spartans") == 1.0
assert ns("Tampa Bay", "Tampa Bay Rays") == 1.0
print("NAME TESTS PASSED")
