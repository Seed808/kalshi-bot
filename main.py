#!/usr/bin/env python3
"""
Kalshi sports bot — 5-step pipeline, PAPER TRADING + ALERTS ONLY (never places real orders).

  1 scan      all open Kalshi markets filtered by volume/time, flags big moves + wide spreads;
              sports game markets matched to de-vigged sportsbook consensus
  2 research  ESPN RSS, Reddit, X in parallel -> mentions, VADER sentiment, injury hits
  3 predict   consensus probability, or XGBoost once trained AND proven better;
              confidence gate: edge must hold under both model and consensus
  4 risk      daily loss limit, trade/day + open-trade caps, fractional Kelly sizing
  5 learn     postmortem on every loss (5 checks), tightens settings when a pattern loses

Commands:
  python main.py series                 list Kalshi Sports series (verify tickers)
  python main.py anomalies              step 1 scan of all markets
  python main.py scan                   full pipeline, print only (no paper trades)
  python main.py paper                  one full cycle with paper trades + alerts
  python main.py run --interval 1800    loop forever
  python main.py settle                 settle trades, label training data, run postmortems
  python main.py train                  train/evaluate the XGBoost model
  python main.py report                 performance, calibration, CLV, postmortems, learning
  python main.py postmortem             recent loss postmortems
"""
import argparse
import json
import os
import sys
import time

import requests

from common import (KALSHI, load_config, now_utc, hst, fnum, fee_per_contract, order_fee,
                    http_get, parse_ts)
from store import Store
import scanner
import research
import model
import risk
import postmortem


# ---------------------------------------------------------------- alerts

def alert(text):
    print(text)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": text[:1900]}, timeout=10)
        except requests.RequestException as e:
            print(f"Discord alert failed: {e}")
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=text.encode(), timeout=10)
        except requests.RequestException as e:
            print(f"ntfy alert failed: {e}")


# ---------------------------------------------------------------- settlement

def settle_all(cfg, store):
    settled = 0
    opens = store.open_trades()
    if opens:
        ms = {m["ticker"]: m for m in scanner.markets_by_tickers([t["market_ticker"] for t in opens])}
        for t in opens:
            m = ms.get(t["market_ticker"], {})
            res = m.get("result")
            if res not in ("yes", "no"):
                continue
            won = res == t["side"]
            pnl = (t["contracts"] if won else 0) - t["contracts"] * t["price"] - t["fee"]
            clv = (t["last_seen_price"] - t["price"]) if t["last_seen_price"] is not None else None
            store.x("UPDATE trades SET status='settled', result=?, pnl=?, clv=?, settled_ts=? WHERE id=?",
                    (res, round(pnl, 4), clv, now_utc().isoformat(), t["id"]))
            settled += 1
    tickers = store.unlabeled_snapshot_tickers()
    labeled = 0
    if tickers:
        for m in scanner.markets_by_tickers(tickers):
            if m.get("result") in ("yes", "no"):
                store.label_snapshot(m["ticker"], 1 if m["result"] == "yes" else 0)
                labeled += 1
    if settled or labeled:
        print(f"Settled {settled} paper trade(s); labeled {labeled} training snapshot(s).")
    return settled


def update_clv(store, all_markets):
    now = now_utc()
    for t in store.open_trades():
        m = all_markets.get(t["market_ticker"])
        if not m or not parse_ts(t["start_ts"]) or parse_ts(t["start_ts"]) <= now:
            continue
        if t["side"] == "yes":
            px = fnum(m.get("yes_ask_dollars"))
        else:
            px = fnum(m.get("no_ask_dollars")) or (1 - fnum(m.get("yes_bid_dollars")))
        if 0 < px < 1:
            store.x("UPDATE trades SET last_seen_price=? WHERE id=?", (px, t["id"]))


# ---------------------------------------------------------------- step 3 decision

def side_quotes(m):
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    no_ask = fnum(m.get("no_ask_dollars")) or (round(1 - yb, 4) if yb > 0 else 0.0)
    # buying NO matches resting YES bids, so NO-ask depth = YES-bid depth
    return [("yes", ya, fnum(m.get("yes_ask_size_fp"))), ("no", no_ask, fnum(m.get("yes_bid_size_fp")))]


def precandidate(game, cfg):
    """Cheap consensus-only check so research is spent only on games near an edge."""
    edge = cfg["series_min_edge"].get(game["series"], cfg["min_edge"]) * 0.5
    for m in game["markets"]:
        p = game["oe"]["fair"][game["mapping"][m["ticker"]]]
        for side, ask, _ in side_quotes(m):
            q = p if side == "yes" else 1 - p
            if 0 < ask < 1 and q - (ask + fee_per_contract(ask, game["fee_mult"])) >= edge:
                return True
    return False


def evaluate_game(game, feats, predictor, cfg, res):
    oe = game["oe"]
    if oe["book_std"] > cfg["max_book_std"]:
        return None, "sportsbooks disagree"
    if oe["staleness_min"] is not None and oe["staleness_min"] > cfg["max_odds_staleness_min"]:
        return None, "odds stale"
    min_edge = cfg["series_min_edge"].get(game["series"], cfg["min_edge"])
    best, reason = None, "no edge"
    for m in game["markets"]:
        f = feats[m["ticker"]]
        if f["spread"] <= 0 or f["spread"] > cfg["max_spread"]:
            continue
        team = game["mapping"][m["ticker"]]
        opp = scanner.opponent(game, team)
        cons = f["fair"]
        mp = predictor.predict(f)
        p = mp if mp is not None else cons
        for side, ask, size in side_quotes(m):
            if not (0 < ask < 1) or size < cfg["min_size"]:
                continue
            cost = ask + fee_per_contract(ask, game["fee_mult"])
            q, qc = (p, cons) if side == "yes" else (1 - p, 1 - cons)
            ev = q - cost
            if ev < min_edge:
                continue
            if mp is not None and qc - cost <= 0:
                reason = "confidence gate: model and consensus disagree"
                continue
            side_team, other = (team, opp) if side == "yes" else (opp, team)
            rs, ro = res.get(side_team, {}), res.get(other, {})
            if rs.get("inj", 0) >= cfg["injury_veto_count"] and rs.get("inj", 0) > ro.get("inj", 0):
                reason = f"research veto: {rs['inj']} injury mentions for {side_team}"
                continue
            if not best or ev > best["ev"]:
                best = {"event_ticker": game["event_ticker"], "series": game["series"],
                        "matchup": game["matchup"], "start": game["start"], "market": m,
                        "team": side_team, "side": side, "ask": ask, "cost": cost, "size": size,
                        "prob": q, "consensus": qc,
                        "model_prob": (mp if side == "yes" else 1 - mp) if mp is not None else None,
                        "ev": ev, "n_books": oe["n_books"], "book_std": oe["book_std"],
                        "staleness": oe["staleness_min"], "fee_mult": game["fee_mult"],
                        "research": {"side": rs, "opp": ro}}
    return best, (None if best else reason)


def fmt_signal(s, n=None):
    m = s["market"]
    rs, ro = s["research"]["side"], s["research"]["opp"]
    src = f'model {s["model_prob"]:.3f} / consensus {s["consensus"]:.3f}' if s["model_prob"] is not None \
        else f'consensus {s["consensus"]:.3f}'
    lines = [f'{s["matchup"]} | {hst(s["start"])}',
             f'  BUY {s["side"].upper()} on "{m["yes_sub_title"]}" @ ${s["ask"]:.2f} '
             f'(w/ fee ${s["cost"]:.4f}) -> backing {s["team"]}',
             f'  prob {s["prob"]:.3f} ({src}, {s["n_books"]} books) | EV +${s["ev"]:.3f}/contract',
             f'  research {s["team"]}: {rs.get("mentions", 0)} mentions, sent {rs.get("sent", 0):+.2f}, '
             f'inj {rs.get("inj", 0)} | opp: {ro.get("mentions", 0)} mentions, '
             f'sent {ro.get("sent", 0):+.2f}, inj {ro.get("inj", 0)}',
             f'  {m["ticker"]}' + (f' | PAPER {n} contracts' if n else '')]
    return "\n".join(lines)


# ---------------------------------------------------------------- cycle

def cycle(cfg_path, store, paper, verbose):
    cfg = load_config(cfg_path)
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        sys.exit("Set ODDS_API_KEY (free key at the-odds-api.com).")

    # step 5 first: close the loop on anything that finished
    settle_all(cfg, store)
    for r, flags, verdict in postmortem.run_postmortems(cfg, store):
        alert(f"POSTMORTEM loss on {r['matchup']} ({r['side'].upper()} {r['team']}): {verdict}")
    changes = postmortem.learn(cfg, store)
    if changes:
        alert("LEARNING UPDATE\n  " + "\n  ".join(changes))
        cfg = load_config(cfg_path)
    model.maybe_retrain(cfg, store)

    # step 1a: whole-exchange anomaly scan
    try:
        an = scanner.anomaly_scan(cfg)
        if verbose:
            print(f"Scan: {an['total']} open markets, {an['passed']} pass volume/time filters, "
                  f"{len(an['flagged'])} flagged")
            for a in an["flagged"][:10]:
                print(f"  {a['ticker']}: {', '.join(a['flags'])} | vol24 {a['vol24']:.0f}")
        if cfg["alert_anomalies"] and an["flagged"]:
            alert("ANOMALIES\n" + "\n".join(f"  {a['ticker']}: {', '.join(a['flags'])}"
                                           for a in an["flagged"][:10]))
    except Exception as e:
        print(f"anomaly scan error: {e}")

    # step 1b: sports games
    games, all_markets = scanner.sports_games(cfg, api_key, store, verbose)
    update_clv(store, all_markets)

    # step 2: research only games near an edge; others use cache
    cands = [g for g in games if precandidate(g, cfg)]
    res = research.cached_research(games, cfg, store)
    if cands:
        res.update(research.research_games(cands, cfg, store))
    if verbose:
        print(f"{len(games)} pregame games | {len(cands)} researched")

    # step 3 + 4
    predictor = model.Predictor(cfg)
    series_idx = {s: i for i, s in enumerate(cfg["series"])}
    signals, opened = [], 0
    for g in games:
        feats = {m["ticker"]: scanner.market_features(g, m, res, series_idx.get(g["series"], -1))
                 for m in g["markets"]}
        for m in g["markets"]:
            store.upsert_snapshot(m["ticker"], g["event_ticker"], g["series"], g["start"],
                                  feats[m["ticker"]], feats[m["ticker"]]["fair"])
        sig, why = evaluate_game(g, feats, predictor, cfg, res)
        if not sig:
            if verbose and why and why != "no edge":
                print(f"  skip {g['matchup']}: {why}")
            continue
        signals.append(sig)
        if not paper:
            print(fmt_signal(sig))
            continue
        n, block = risk.check_and_size(sig, cfg, store)
        if block:
            print(f"  RISK BLOCK {g['matchup']}: {block}")
            continue
        fee = order_fee(sig["ask"], n, sig["fee_mult"])
        store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,
                   contracts,prob,consensus,model_prob,ev,n_books,book_std,staleness_min,research,start_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_utc().isoformat(), sig["market"]["ticker"], sig["event_ticker"], sig["series"],
                 sig["matchup"], sig["team"], sig["side"], sig["ask"], fee, n, sig["prob"],
                 sig["consensus"], sig["model_prob"], sig["ev"], sig["n_books"], sig["book_std"],
                 sig["staleness"], json.dumps(sig["research"]), sig["start"].isoformat()))
        opened += 1
        alert("PAPER SIGNAL\n" + fmt_signal(sig, n))
    mode = "Predictor: XGBoost" if predictor.active else "Predictor: consensus"
    print(f"{hst(now_utc())} | {mode} | {len(signals)} signal(s), {opened} new paper trade(s)")


# ---------------------------------------------------------------- report

def report(cfg, store):
    bankroll, exposure = store.bankroll(cfg)
    rows = store.settled_trades()
    print(f"Paper bankroll ${bankroll:,.2f} (start ${cfg['bankroll']:,.2f}) | open {store.open_count()} "
          f"| exposure ${exposure:,.2f}")
    meta = model.load_meta(cfg)
    print(f"Model: {'ACTIVE' if meta.get('active') else 'off'} | labeled samples "
          f"{len(store.labeled_snapshots())}" + (f" | {meta['reason']}" if meta.get("reason") else ""))
    if rows:
        st = postmortem._stats(rows)
        clvs = [r["clv"] for r in rows if r["clv"] is not None]
        print(f"Settled {st['n']} | win rate {st['actual']:.1%} vs model-expected {st['expected']:.1%} "
              f"| ROI {st['roi']:.1%}" + (f" | avg CLV {sum(clvs)/len(clvs):+.3f}" if clvs else ""))
        brier = sum((r["prob"] - (1 if r["result"] == r["side"] else 0)) ** 2 for r in rows) / len(rows)
        print(f"Brier {brier:.4f}")
        print("Calibration (prob bucket -> actual):")
        for lo in [0.0, 0.2, 0.4, 0.6, 0.8]:
            b = [r for r in rows if lo <= r["prob"] < lo + 0.2]
            if b:
                w = sum(1 for r in b if r["result"] == r["side"])
                print(f"  {lo:.1f}-{lo + 0.2:.1f}: n={len(b)} actual {w / len(b):.1%}")
        if len(rows) < 100:
            print("Note: under 100 settled trades — results are mostly noise.")
    else:
        print("No settled trades yet.")
    counts = {}
    for p in store.q("SELECT flags FROM postmortems"):
        for f in json.loads(p["flags"]) or ["variance"]:
            counts[f] = counts.get(f, 0) + 1
    if counts:
        print("Postmortem causes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for r in store.q("SELECT ts, change FROM learn_log ORDER BY ts DESC LIMIT 10"):
        print(f"  learned {hst(parse_ts(r['ts']))}: {r['change']}")


def show_postmortems(store):
    for r in store.q("""SELECT p.ts, p.verdict, t.matchup, t.side, t.team FROM postmortems p
                        JOIN trades t ON t.id=p.trade_id ORDER BY p.ts DESC LIMIT 20"""):
        print(f"{hst(parse_ts(r['ts']))} | {r['matchup']} {r['side'].upper()} {r['team']}: {r['verdict']}")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["series", "anomalies", "scan", "paper", "run", "settle", "train",
                                    "report", "postmortem"])
    ap.add_argument("--interval", type=int, default=1800)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config)
    store = Store(cfg["db_path"])

    if a.cmd == "series":
        for s in http_get(f"{KALSHI}/series", {"category": "Sports"}).json().get("series", []):
            print(f'{s.get("ticker"):<24} {s.get("title")}')
    elif a.cmd == "anomalies":
        an = scanner.anomaly_scan(cfg)
        print(f"{an['total']} open | {an['passed']} pass filters | {len(an['flagged'])} flagged")
        for x in an["flagged"][:30]:
            print(f"  {x['ticker']}: {', '.join(x['flags'])} | bid {x['bid']:.2f} ask {x['ask']:.2f} "
                  f"| vol24 {x['vol24']:.0f} | resolves {hst(x['resolves'])}")
    elif a.cmd == "scan":
        cycle(a.config, store, paper=False, verbose=True)
    elif a.cmd == "paper":
        cycle(a.config, store, paper=True, verbose=a.verbose)
    elif a.cmd == "run":
        print(f"Running every {a.interval}s — PAPER ONLY, no real orders. Ctrl+C to stop.")
        while True:
            try:
                cycle(a.config, store, paper=True, verbose=a.verbose)
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception as e:
                print(f"cycle error: {e}")
            time.sleep(a.interval)
    elif a.cmd == "settle":
        settle_all(cfg, store)
        for r, flags, verdict in postmortem.run_postmortems(cfg, store):
            print(f"POSTMORTEM {r['matchup']}: {verdict}")
        for c in postmortem.learn(cfg, store):
            print("LEARNED:", c)
    elif a.cmd == "train":
        model.train(cfg, store)
    elif a.cmd == "report":
        report(cfg, store)
    elif a.cmd == "postmortem":
        show_postmortems(store)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
