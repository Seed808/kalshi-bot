"""Offline end-to-end test with mocked Kalshi, Odds API and research sources."""
import json
import os
import random
import shutil
import sys
import tempfile
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common, scanner, research, model, postmortem, risk, main  # noqa: E402
from store import Store  # noqa: E402

work = tempfile.mkdtemp()
os.chdir(work)
open("config.json", "w").write(json.dumps({"mode": "edge", "odds_spend_horizon_hours": 12, "daily_loss_limit_pct": 0.10, "max_open_trades": 8, "max_trades_per_day": 10, "max_open_exposure_pct": 0.40}))  # this test covers edge mode
os.environ["ODDS_API_KEY"] = "test"
for k in ("X_BEARER_TOKEN", "REDDIT_CLIENT_ID", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC"):
    os.environ.pop(k, None)

# ---- math sanity
assert common.order_fee(0.55, 100) == 1.74
assert abs(common.fee_per_contract(0.5) - 0.0175) < 1e-12

now = common.now_utc()
start = now + timedelta(hours=6)
d = (start - timedelta(hours=5)).strftime("%y%b%d").upper()
E1, E2 = f"KXNBAGAME-{d}LALBOS", f"KXNBAGAME-{d}NYKMIA"
iso_lu = (now - timedelta(minutes=5)).isoformat()


def book(h, hp, a, ap):
    return {"last_update": iso_lu, "markets": [{"key": "h2h", "last_update": iso_lu, "outcomes": [
        {"name": h, "price": hp}, {"name": a, "price": ap}]}]}


ODDS_EVENTS = [
    {"home_team": "Boston Celtics", "away_team": "Los Angeles Lakers", "commence_time": start.isoformat(),
     "bookmakers": [book("Boston Celtics", -200, "Los Angeles Lakers", 170)] * 4},
    {"home_team": "Miami Heat", "away_team": "New York Knicks", "commence_time": start.isoformat(),
     "bookmakers": [book("Miami Heat", 120, "New York Knicks", -140)] * 4},
]


def mk(t, et, name, yb, ya):
    return {"ticker": t, "event_ticker": et, "yes_sub_title": name, "yes_bid_dollars": f"{yb:.4f}",
            "yes_ask_dollars": f"{ya:.4f}", "yes_ask_size_fp": "500.00", "yes_bid_size_fp": "400.00",
            "no_ask_dollars": f"{1 - yb:.4f}", "volume_24h_fp": "2000.00",
            "last_price_dollars": f"{ya:.4f}", "previous_price_dollars": f"{ya:.4f}",
            "expected_expiration_time": (start + timedelta(hours=3)).isoformat()}


MARKETS = {
    E1 + "-LAL": mk(E1 + "-LAL", E1, "Los Angeles L", 0.28, 0.30),   # Lakers underpriced (~0.357 fair)
    E1 + "-BOS": mk(E1 + "-BOS", E1, "Boston", 0.69, 0.71),
    E2 + "-NYK": mk(E2 + "-NYK", E2, "New York", 0.47, 0.49),         # Knicks underpriced (~0.565 fair)
    E2 + "-MIA": mk(E2 + "-MIA", E2, "Miami", 0.50, 0.52),
}
ANOM = dict(mk("KXCPI-26NOV-T3", "KXCPI-26NOV", "Above 3%", 0.30, 0.45),
            last_price_dollars="0.4000", previous_price_dollars="0.2000", title="CPI above 3%?")
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
            out = []
            for t in params["tickers"].split(","):
                m = dict(MARKETS.get(t, {"ticker": t}))
                m["result"] = RESULTS.get(t, "")
                out.append(m)
            return R({"markets": out, "cursor": ""})
        if params.get("series_ticker") == "KXNBAGAME":
            return R({"markets": list(MARKETS.values()), "cursor": ""})
        if params.get("series_ticker"):
            return R({"markets": [], "cursor": ""})
        return R({"markets": list(MARKETS.values()) + [ANOM], "cursor": ""})   # anomaly scan
    if "/series/" in url:
        return R({"series": {"fee_multiplier": 1}})
    if "basketball_nba/odds" in url:
        return R(json.loads(json.dumps(ODDS_EVENTS)), {"x-requests-remaining": "480"})
    return R([])


RSS = [{"src": "rss", "text": "Knicks star Jalen ruled out Tuesday with injury"},
       {"src": "rss", "text": "New York Knicks guard questionable, injured ankle"},
       {"src": "rss", "text": "Lakers look great in practice, confident ahead of Boston"}]

patches = [mock.patch.object(scanner, "http_get", fake_get), mock.patch.object(main, "http_get", fake_get),
           mock.patch.object(research, "fetch_rss", lambda urls, lb: RSS if urls else []),
           mock.patch.object(research, "fetch_reddit", lambda subs, lb: []),
           mock.patch.object(research, "fetch_x", lambda team, cfg: [])]
for p in patches:
    p.start()

cfg = common.load_config()
store = Store(cfg["db_path"])

# ---- step 1a anomaly scan
an = scanner.anomaly_scan(cfg)
assert any(a["ticker"] == "KXCPI-26NOV-T3" for a in an["flagged"]), an
print("step1 anomalies:", [(a["ticker"], a["flags"]) for a in an["flagged"]])

# ---- full cycle (steps 1-4)
main.cycle("config.json", store, paper=True, verbose=True)
html = open("docs/index.html").read()
assert "Kalshi price board" in html and "buy <b>YES</b> on Los Angeles Lakers" in html and "1 play right now" in html and "Paper size: 49 contracts" in html, "site not rendered"
b = json.load(open("docs/board.json"))["board"]
lal = [x for g in b for x in g["sides"] if x["name"] == "Los Angeles Lakers"][0]
sig = [g["signal"] for g in b if g["signal"]][0]
assert abs(lal["ev"] - sig["ev"]) < 1e-9, (lal, sig)   # board agrees with the signal
print("site OK:", len(html), "bytes;", len(b), "games on board")
trades = store.open_trades()
assert len(trades) == 1 and trades[0]["market_ticker"] == E1 + "-LAL" and trades[0]["side"] == "yes", \
    [dict(t) for t in trades]
res = json.loads(trades[0]["research"])
assert res["side"]["mentions"] >= 1  # Lakers RSS item scored
print("step2 research knicks:", store.get_research("New York Knicks", 60))
assert store.get_research("New York Knicks", 60)["inj"] >= 2  # Knicks vetoed by injury news
assert len(store.q("SELECT * FROM snapshots")) == 4

# re-run: no duplicate position, CLV tracking updates
MARKETS[E1 + "-LAL"] = mk(E1 + "-LAL", E1, "Los Angeles L", 0.23, 0.25)  # market moves against us
main.cycle("config.json", store, paper=True, verbose=False)
assert store.open_count() == 1
t = store.open_trades()[0]
assert abs(t["last_seen_price"] - 0.25) < 1e-9, t["last_seen_price"]

# ---- settle as a loss -> postmortem (step 5)
store.x("UPDATE trades SET start_ts=?", ((now - timedelta(hours=4)).isoformat(),))
store.x("UPDATE snapshots SET start_ts=?", ((now - timedelta(hours=4)).isoformat(),))
RESULTS.update({E1 + "-LAL": "no", E1 + "-BOS": "yes", E2 + "-NYK": "yes", E2 + "-MIA": "no"})
main.settle_all(cfg, store)
pms = postmortem.run_postmortems(cfg, store)
assert len(pms) == 1 and "negative_clv" in pms[0][1], pms
print("step5 postmortem:", pms[0][1], "|", pms[0][2])
assert len(store.labeled_snapshots()) == 4

# ---- learning: 20 thin-edge NBA losses -> raise min_edge and block series
for i in range(20):
    store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,contracts,
               prob,consensus,ev,n_books,book_std,staleness_min,research,start_ts,status,result,pnl,settled_ts,
               postmortem_done) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (now.isoformat(), f"T{i}", f"EV{i}", "KXNBAGAME", "A @ B", "A", "yes", 0.5, 0.18, 10,
             0.55, 0.55, 0.033, 4, 0.01, 5, "{}", now.isoformat(), "settled", "no", -5.18, now.isoformat()))
changes = postmortem.learn(cfg, store)
print("step5 learning:", changes)
cfg2 = common.load_config()
assert cfg2["series_min_edge"]["KXNBAGAME"] > cfg["min_edge"]
assert "KXNBAGAME" in cfg2["blocked_series"]
assert cfg2["min_edge"] == cfg["min_edge"]  # global rule needs >=30 trades; only 21 here

# ---- step 4 risk: daily loss limit blocks new trades now
sig = {"event_ticker": "NEW", "prob": 0.6, "cost": 0.5, "size": 100}
n, why = risk.check_and_size(sig, cfg2, store)
assert n == 0 and why, (n, why)
print("step4 risk block:", why)

# ---- step 3 model: synthetic data with a real signal in sent_diff
store.x("DELETE FROM snapshots")
random.seed(1)
for i in range(1000):
    fair = random.uniform(0.2, 0.8)
    sd = random.uniform(-0.5, 0.5)
    p_true = min(0.97, max(0.03, fair + 0.6 * sd))  # strong planted signal
    f = {k: 0.0 for k in scanner.FEATURES}
    f.update(fair=fair, mid=fair, sent_diff=sd, has_research=1.0, n_books=4)
    store.x("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)",
            (f"M{i}", f"G{i}", "KXNBAGAME", now.isoformat(), now.isoformat(), json.dumps(f), fair,
             1 if random.random() < p_true else 0))
meta = model.train(cfg2, store)
assert meta["active"], meta
pred = model.Predictor(cfg2)
f_hi = {k: 0.0 for k in scanner.FEATURES}
f_hi.update(fair=0.5, mid=0.5, sent_diff=0.45, has_research=1.0, n_books=4)
f_lo = dict(f_hi, sent_diff=-0.45)
p_hi, p_lo = pred.predict(f_hi), pred.predict(f_lo)
assert p_hi > p_lo, (p_hi, p_lo)
print(f"step3 model: logloss {meta['model_logloss']:.4f} vs consensus {meta['baseline_logloss']:.4f}; "
      f"p(sent+)={p_hi:.3f} p(sent-)={p_lo:.3f}")

main.report(cfg2, store)
for p in patches:
    p.stop()
shutil.rmtree(work, ignore_errors=True)
print("\nALL PIPELINE TESTS PASSED")
