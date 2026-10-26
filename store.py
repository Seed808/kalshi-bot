"""SQLite storage: paper trades, feature snapshots, postmortems, research cache, X read budget."""
import json
import sqlite3
from datetime import timedelta

from common import now_utc, parse_ts, hst_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, ts TEXT, market_ticker TEXT, event_ticker TEXT, series TEXT,
  matchup TEXT, team TEXT, side TEXT, price REAL, fee REAL, contracts INTEGER,
  prob REAL, consensus REAL, model_prob REAL, ev REAL, n_books INTEGER, book_std REAL,
  staleness_min REAL, research TEXT, start_ts TEXT, last_seen_price REAL,
  status TEXT DEFAULT 'open', result TEXT, pnl REAL, clv REAL, settled_ts TEXT,
  postmortem_done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshots(
  market_ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, ts TEXT, start_ts TEXT,
  features TEXT, consensus REAL, label INTEGER);
CREATE TABLE IF NOT EXISTS postmortems(
  id INTEGER PRIMARY KEY, trade_id INTEGER, ts TEXT, flags TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS research_cache(team TEXT PRIMARY KEY, ts TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS x_reads(ts TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS learn_log(ts TEXT, change TEXT);
CREATE TABLE IF NOT EXISTS odds_cache(sport TEXT PRIMARY KEY, ts TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS odds_calls(ts TEXT, sport TEXT, cost REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        cur = self.con.execute(sql, args)
        self.con.commit()
        return cur

    # ---- bankroll / limits
    def bankroll(self, cfg):
        realized = self.q("SELECT COALESCE(SUM(pnl),0) v FROM trades WHERE status='settled'")[0]["v"]
        exposure = self.q("SELECT COALESCE(SUM(price*contracts+fee),0) v FROM trades "
                          "WHERE status='open'")[0]["v"]
        return cfg["bankroll"] + realized, exposure

    def today_pnl(self):
        today = hst_date(now_utc())
        return sum(r["pnl"] or 0 for r in self.q(
            "SELECT pnl, settled_ts FROM trades WHERE status='settled'")
            if r["settled_ts"] and hst_date(parse_ts(r["settled_ts"])) == today)

    def trades_today(self):
        today = hst_date(now_utc())
        return sum(1 for r in self.q("SELECT ts FROM trades") if hst_date(parse_ts(r["ts"])) == today)

    def open_count(self):
        return self.q("SELECT COUNT(*) c FROM trades WHERE status='open'")[0]["c"]

    def open_on_event(self, event_ticker):
        return bool(self.q("SELECT 1 FROM trades WHERE event_ticker=? AND status='open'", (event_ticker,)))

    def open_trades(self):
        return self.q("SELECT * FROM trades WHERE status='open'")

    def settled_trades(self, limit=None):
        sql = "SELECT * FROM trades WHERE status='settled' ORDER BY settled_ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.q(sql)

    # ---- snapshots (training data)
    def upsert_snapshot(self, m_ticker, event_ticker, series, start, features, consensus):
        self.x("""INSERT INTO snapshots(market_ticker,event_ticker,series,ts,start_ts,features,consensus)
                  VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(market_ticker) DO UPDATE SET ts=excluded.ts, features=excluded.features,
                  consensus=excluded.consensus WHERE snapshots.label IS NULL""",
               (m_ticker, event_ticker, series, now_utc().isoformat(), start.isoformat(),
                json.dumps(features), consensus))

    def unlabeled_snapshot_tickers(self, older_than_hours=3):
        cutoff = now_utc() - timedelta(hours=older_than_hours)
        return [r["market_ticker"] for r in self.q("SELECT market_ticker, start_ts FROM snapshots "
                                                   "WHERE label IS NULL")
                if parse_ts(r["start_ts"]) and parse_ts(r["start_ts"]) < cutoff]

    def label_snapshot(self, ticker, label):
        self.x("UPDATE snapshots SET label=? WHERE market_ticker=?", (label, ticker))

    def labeled_snapshots(self):
        return self.q("SELECT * FROM snapshots WHERE label IS NOT NULL")

    # ---- research cache
    def get_research(self, team, ttl_min):
        r = self.q("SELECT ts, data FROM research_cache WHERE team=?", (team,))
        if r and now_utc() - parse_ts(r[0]["ts"]) < timedelta(minutes=ttl_min):
            return json.loads(r[0]["data"])
        return None

    def put_research(self, team, data):
        self.x("INSERT OR REPLACE INTO research_cache(team,ts,data) VALUES(?,?,?)",
               (team, now_utc().isoformat(), json.dumps(data)))

    # ---- Odds API free-plan budget
    def odds_credits_used(self, now):
        month = today = 0.0
        for r in self.q("SELECT ts, cost FROM odds_calls"):
            d = parse_ts(r["ts"])
            if d.year == now.year and d.month == now.month:
                month += r["cost"]
                if d.date() == now.date():
                    today += r["cost"]
        return month, today

    def log_odds_call(self, sport, cost):
        self.x("INSERT INTO odds_calls(ts,sport,cost) VALUES(?,?,?)", (now_utc().isoformat(), sport, cost))

    def get_odds_cache(self, sport):
        r = self.q("SELECT ts, data FROM odds_cache WHERE sport=?", (sport,))
        if not r:
            return None
        return (now_utc() - parse_ts(r[0]["ts"])).total_seconds() / 60, json.loads(r[0]["data"])

    def put_odds_cache(self, sport, raw):
        self.x("INSERT OR REPLACE INTO odds_cache(sport,ts,data) VALUES(?,?,?)",
               (sport, now_utc().isoformat(), json.dumps(raw)))

    def get_meta(self, k):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0]["v"] if r else None

    def set_meta(self, k, v):
        self.x("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    # ---- X budget
    def x_reads_this_month(self):
        n = now_utc()
        return sum(r["n"] for r in self.q("SELECT ts, n FROM x_reads")
                   if (lambda d: d.year == n.year and d.month == n.month)(parse_ts(r["ts"])))

    def add_x_reads(self, n):
        self.x("INSERT INTO x_reads(ts,n) VALUES(?,?)", (now_utc().isoformat(), n))
