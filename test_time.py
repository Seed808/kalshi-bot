"""Times on the site are always right and always HST:
- Python side: UTC -> Pacific/Honolulu conversion (fixed UTC-10, no DST)
- Browser side: opened on a device set to New York time, at fake clock moments, the page shows
  HST times, flips "Starts" -> "Started" on time, labels estimated/tennis times, and warns when stale.
"""
import os
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common, site_builder  # noqa: E402
from store import Store  # noqa: E402

# ---- conversion: the Osaka match, 01:30 UTC Thursday = 3:30 PM Wednesday in Hawaii
d = datetime(2026, 10, 29, 1, 30, tzinfo=timezone.utc)
assert common.hst(d) == "Wed 10/28 03:30 PM HST", common.hst(d)
assert site_builder.when(d.isoformat()) == "Wed 3:30 PM"
# Hawaii has no DST: the offset is -10h in summer and winter (mainland clocks change Nov 1, 2026)
for month in (1, 7, 11):
    t = datetime(2026, month, 15, 12, 0, tzinfo=timezone.utc)
    assert t.astimezone(common.HST).utcoffset() == timedelta(hours=-10)

work = tempfile.mkdtemp()
os.chdir(work)
cfg = common.load_config()
store = Store(cfg["db_path"])
T0 = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)   # Wed 12:00 PM HST: when the page is built


def add(team, series, start, books):
    store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,contracts,prob,
               consensus,ev,n_books,book_std,staleness_min,research,start_ts,pick_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (T0.isoformat(), f"M-{team}", f"E-{team}", series, f"{team} vs Opp", team, "yes", 0.7, 0.15, 12,
             0.75, 0.75, 0.05, books, 0.01, 5, "{}", start.isoformat(), "favorite"))


add("Naomi Osaka", "KXWTAMATCH", d, 6)                                  # tennis, Wed 3:30 PM HST
add("Thunder", "KXNBAGAME", T0 + timedelta(hours=4, minutes=40), 9)     # Wed 4:40 PM HST, sportsbook time
add("Vancouver", "KXMLSGAME", T0 + timedelta(hours=5, minutes=30), 0)   # Wed 5:30 PM HST, estimated

orig_now = site_builder.now_utc
site_builder.now_utc = lambda: T0
site_builder.render(cfg, store, [], None, False, [])
site_builder.now_utc = orig_now
html = open("docs/index.html").read()
# no-JavaScript fallback text, built at Wed 12:00 PM HST
assert "Scheduled Wed 3:30 PM HST (in 3h 30m; tennis order of play can slip)." in html
assert "Starts Wed 4:40 PM HST (in 4h 40m)." in html
assert "Starts about Wed 5:30 PM HST (in 5h 30m; estimated from Kalshi)." in html

from playwright.sync_api import sync_playwright  # noqa: E402
url = pathlib.Path("docs/index.html").resolve().as_uri()


def view(at, tz="America/New_York"):
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(timezone_id=tz)
        pg = ctx.new_page()
        pg.clock.install(time=at)
        pg.goto(url)
        pg.wait_for_timeout(200)
        out = {"now": pg.inner_text("#now"), "ago": pg.inner_text("#ago"),
               "stale": pg.is_visible("#stale"),
               "starts": [e.inner_text() for e in pg.query_selector_all("[data-start]")],
               "device_tz": pg.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")}
        b.close()
        return out


# 20 minutes after the build (Wed 12:20 PM HST), device in New York
v = view(T0 + timedelta(minutes=20))
print(v)
assert v["device_tz"] == "America/New_York"
assert v["now"] == "Wed 10/28 12:20 PM HST" and v["ago"] == " (20m ago)" and not v["stale"]
assert "Scheduled Wed 3:30 PM HST (in 3h 10m; tennis order of play can slip)." in v["starts"]

# Wed 6:26 PM HST, the moment of the complaint: page built at noon, so it must say it's stale,
# and every game whose start has passed must say "Started"/"Scheduled for", never "Starts"
v = view(datetime(2026, 10, 29, 4, 26, tzinfo=timezone.utc), tz="Asia/Tokyo")
print(v)
assert v["now"] == "Wed 10/28 06:26 PM HST", v["now"]
assert v["stale"] is True
assert "Scheduled for Wed 3:30 PM HST (2h 56m ago), waiting for the result." in v["starts"]
assert "Started Wed 4:40 PM HST (1h 46m ago), waiting for the result." in v["starts"]
assert "Started about Wed 5:30 PM HST (56m ago), waiting for the result." in v["starts"]
assert not any(s.startswith("Starts") for s in v["starts"])

shutil.rmtree(work, ignore_errors=True)
print("TIME TESTS PASSED")
