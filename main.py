#!/usr/bin/env python3
"""
Kalshi sports bot — PAPER TRADING + ALERTS ONLY (never places real orders).

Default "picks" mode (config "mode": "picks"): for every matched game starting in the next 36h,
back the side the sportsbooks favor (rule lives in picks.py), with a seeded ~25% upset coin and an
injury-news override. Fixed-dollar paper stakes, same risk caps. The 5-step "edge" pipeline below
still runs when "mode" is "edge".

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
from datetime import timedelta
import json
import os
import sys
import time

import requests

from common import (KALSHI, install_redaction, load_config, now_utc, hst, fnum, fee_per_contract, order_fee,
                    http_get, parse_ts)
from store import Store
import scanner
import research
import model
import risk
import postmortem
import picks
import site_builder as site


# ---------------------------------------------------------------- alerts

def alert(text):
    print(text)
    tg_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    tg_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if tg_token and tg_chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                              json={"chat_id": tg_chat, "text": "[Kalshi bot] " + text[:3900],
                                    "disable_web_page_preview": True}, timeout=10)
            if r.status_code != 200:
                try:
                    why = r.json().get("description", "")
                except ValueError:
                    why = ""
                print(f"Telegram alert failed: HTTP {r.status_code} {why}".rstrip())
                if "chat not found" in why.lower():
                    print("  Fix: open your bot in Telegram, press Start, send it a message, then copy the "
                          "chat id from getUpdates again into TELEGRAM_CHAT_ID (numbers only).")
        except requests.RequestException:
            print("Telegram alert failed: network error")  # never print the URL (contains token)
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


def fmt_signal(s, n=None, need=0.03):
    """Plain-language play alert (Telegram/Discord/ntfy and the run log)."""
    from site_builder import max_price
    m = s["market"]
    label = m.get("yes_sub_title") or s["team"]
    if s["side"] == "yes":
        what = f'On Kalshi, buy YES on "{label}".'
    else:
        what = f'On Kalshi, buy NO on "{label}". That\'s a bet on {s["team"]} to win.'
    mp = max_price(s["prob"], need, s.get("fee_mult", 1.0))
    role = "favorite" if s["prob"] >= 0.5 else "underdog"
    rs = (s.get("research") or {}).get("side") or {}
    lines = [what,
             f'It\'s {s["ask"] * 100:.0f}¢ now.' + (f' Pay {mp * 100:.0f}¢ or less.' if mp else ""),
             f'{s["matchup"]}, {hst(s["start"])}',
             f'Sportsbooks give {s["team"]} a {s["prob"] * 100:.1f}% chance ({s["n_books"]} books), so '
             f'{s["team"]} is the {role}. Edge after Kalshi\'s fee: +{s["ev"] * 100:.1f}¢ per contract.']
    if n:
        lines.append(f'Paper size: {n} contracts (about ${n * s["cost"]:,.2f}). If {s["team"]} wins, '
                     f'that pays ${n:,.2f}.')
    if rs.get("mentions"):
        lines.append(f'News check: {rs["mentions"]} recent mention(s) of {s["team"]}, '
                     f'{rs.get("inj", 0)} about injuries.')
    lines.append(f'Kalshi market: {m["ticker"]}')
    lines.append("https://seed808.github.io/kalshi-bot/")
    return "\n".join(lines)


# ---------------------------------------------------------------- cycle

def cycle(cfg_path, store, paper, verbose):
    cfg = load_config(cfg_path)
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        sys.exit("Set ODDS_API_KEY (free key at the-odds-api.com).")

    picks_mode = cfg["mode"] == "picks"
    if picks_mode:
        cfg["min_books"] = cfg["pick_min_books"]

    # step 5 first: close the loop on anything that finished
    settle_all(cfg, store)
    if picks_mode:
        # one message with every result, instead of a message per loss
        done = store.q("SELECT * FROM trades WHERE status='settled' AND postmortem_done=0")
        if done:
            alert("RESULTS\n" + "\n".join(
                f'{"Won" if r["result"] == r["side"] else "Lost"}: {r["team"]} ({r["matchup"]}) '
                f'{"+" if (r["pnl"] or 0) >= 0 else "-"}${abs(r["pnl"] or 0):,.2f}' for r in done)
                + "\nhttps://seed808.github.io/kalshi-bot/")
        postmortem.run_postmortems(cfg, store)
        # no self-tuning in pick mode: it exists to protect an edge, and would otherwise block
        # whole sports after a few losing favorites
    else:
        for r, flags, verdict in postmortem.run_postmortems(cfg, store):
            alert(f"PAPER LOSS REVIEW\n{r['team']} lost ({r['matchup']}).\nWhy: {verdict}")
        changes = postmortem.learn(cfg, store)
        if changes:
            alert("BOT ADJUSTED ITS SETTINGS\n" + "\n".join("- " + c for c in changes))
            cfg = load_config(cfg_path)
    model.maybe_retrain(cfg, store)

    # step 1a: whole-exchange anomaly scan
    an = None
    try:
        an = scanner.anomaly_scan(cfg)
        scanner.enrich_anomalies(an["flagged"], store)
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
    skipped = []
    games, all_markets = scanner.sports_games(cfg, api_key, store, verbose, skipped=skipped)
    update_clv(store, all_markets)

    if picks_mode:
        board = run_picks(games, cfg, store, paper, verbose, skipped)
        try:
            site.render(cfg, store, board, an, False, skipped)
        except Exception as e:   # the website must never break the bot
            print(f"site render error: {e}")
        return

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
    signals, opened, board = [], 0, []
    for g in games:
        feats = {m["ticker"]: scanner.market_features(g, m, res, series_idx.get(g["series"], -1))
                 for m in g["markets"]}
        for m in g["markets"]:
            store.upsert_snapshot(m["ticker"], g["event_ticker"], g["series"], g["start"],
                                  feats[m["ticker"]], feats[m["ticker"]]["fair"])
        sig, why = evaluate_game(g, feats, predictor, cfg, res)
        board.append(board_entry(g, feats, sig, why))
        if not sig:
            if verbose and why and why != "no edge":
                print(f"  skip {g['matchup']}: {why}")
            continue
        signals.append(sig)
        if not paper:
            print(fmt_signal(sig, need=cfg["series_min_edge"].get(sig["series"], cfg["min_edge"])))
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
        alert("PAPER PLAY\n" + fmt_signal(sig, n, cfg["series_min_edge"].get(sig["series"], cfg["min_edge"])))
    mode = "Predictor: XGBoost" if predictor.active else "Predictor: consensus"
    print(f"{hst(now_utc())} | {mode} | {len(signals)} signal(s), {opened} new paper trade(s)")
    try:
        site.render(cfg, store, board, an, predictor.active)
    except Exception as e:   # the website must never break the bot
        print(f"site render error: {e}")


def run_picks(games, cfg, store, paper, verbose, skipped):
    """PICK MODE: one pick per matched game (rule in picks.py), soonest games first."""
    res = research.cached_research(games, cfg, store)
    if cfg["pick_injury_override"] and games:
        res.update(research.research_games(games, cfg, store))
    series_idx = {s: i for i, s in enumerate(cfg["series"])}
    board, new = [], []
    for g in sorted(games, key=lambda g: g["start"]):
        feats = {m["ticker"]: scanner.market_features(g, m, res, series_idx.get(g["series"], -1))
                 for m in g["markets"]}
        for m in g["markets"]:   # keep logging training data
            store.upsert_snapshot(m["ticker"], g["event_ticker"], g["series"], g["start"],
                                  feats[m["ticker"]], feats[m["ticker"]]["fair"])
        sides = []
        for team in g["mapping"].values():
            route = picks.cheapest_route(g, team)
            sides.append({"name": team, "fair": g["oe"]["fair"][team], "ask": route[2] if route else 0.0})
        entry = {"series": g["series"], "event_ticker": g["event_ticker"], "matchup": g["matchup"],
                 "start": g["start"].isoformat(), "books": g["oe"]["n_books"],
                 "odds_age_min": g["oe"]["staleness_min"], "sides": sides, "pick": None, "why": None}
        board.append(entry)

        prior = store.open_trade_for_event(g["event_ticker"])
        if prior:   # picked on an earlier run: show that pick, don't pick again
            entry["pick"] = {"team": prior["team"], "type": prior["pick_type"] or "edge",
                             "prob": prior["prob"], "price": prior["price"],
                             "contracts": prior["contracts"], "status": "open"}
            continue
        p, why = picks.make_pick(g, res, cfg)
        if not p:
            entry["why"] = why
            continue
        entry["pick"] = {"team": p["team"], "type": p["pick_type"], "prob": p["prob"], "price": p["ask"],
                         "contracts": None, "status": "new"}
        if not paper:
            print(picks.fmt_pick(p))
            continue
        n, block = risk.check_pick(p, cfg, store)
        if block:
            entry["pick"]["status"] = "blocked"
            entry["why"] = block
            if verbose:
                print(f"  not opened {g['matchup']}: {block}")
            continue
        fee = order_fee(p["ask"], n, p["fee_mult"])
        store.x("""INSERT INTO trades(ts,market_ticker,event_ticker,series,matchup,team,side,price,fee,
                   contracts,prob,consensus,model_prob,ev,n_books,book_std,staleness_min,research,start_ts,
                   pick_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_utc().isoformat(), p["market"]["ticker"], p["event_ticker"], p["series"],
                 p["matchup"], p["team"], p["side"], p["ask"], fee, n, p["prob"], p["consensus"],
                 None, p["ev"], p["n_books"], p["book_std"], p["staleness"], json.dumps(p["research"]),
                 p["start"].isoformat(), p["pick_type"]))
        entry["pick"].update(status="open", contracts=n)
        new.append((p, n))
    if new:
        alert(f"NEW PICKS ({len(new)})\n\n" + "\n\n".join(
            f"{hst(p['start'])}\n{picks.fmt_pick(p, n)}" for p, n in new)
            + "\n\nhttps://seed808.github.io/kalshi-bot/")
    print(f"{hst(now_utc())} | pick mode | {len(games)} games matched, {len(new)} new pick(s), "
          f"{store.open_count()} open, {len(skipped)} skipped")
    return board


def board_entry(g, feats, sig, why):
    """One row of the public price board: Kalshi YES ask vs sportsbook fair price for each side."""
    # Each side can be backed two ways on Kalshi: YES on its own market, or NO on the opponent's
    # market (separate order books). Show the cheaper one so the board agrees with the signals.
    sides = []
    mk = g["markets"]
    for i, m in enumerate(mk):
        other = mk[1 - i]
        f = feats[m["ticker"]]
        routes = [(fnum(m.get("yes_ask_dollars")), "yes")]
        for side, ask, _size in side_quotes(other):
            if side == "no":
                routes.append((ask, "no on opponent"))
        routes = [(a, r) for a, r in routes if 0 < a < 1]
        if routes:
            ask, route = min(routes)
            ev = f["fair"] - (ask + fee_per_contract(ask, g["fee_mult"]))
        else:
            ask, route, ev = 0.0, "", None
        sides.append({"name": g["mapping"][m["ticker"]], "bid": fnum(m.get("yes_bid_dollars")),
                      "ask": ask, "route": route, "fair": f["fair"], "ev": ev})
    return {"series": g["series"], "event_ticker": g["event_ticker"], "matchup": g["matchup"],
            "start": g["start"].isoformat(),
            "books": g["oe"]["n_books"], "sides": sides, "why": why,
            "signal": ({"team": sig["team"], "side": sig["side"], "ask": sig["ask"], "ev": sig["ev"],
                        "prob": sig["prob"]} if sig else None)}


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
    install_redaction()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["series", "anomalies", "scan", "paper", "run", "settle", "train",
                                    "report", "postmortem", "discover", "test-alert", "sample-alert"])
    ap.add_argument("--q", default="tennis,atp,wta", help="discover: comma-separated keywords")
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
    elif a.cmd == "discover":
        discover(a.q)
    elif a.cmd == "sample-alert":
        start = now_utc() + timedelta(hours=6)
        fake = {"market": {"ticker": "KXMLBGAME-SAMPLE-MIL", "yes_sub_title": "Milwaukee"},
                "team": "Tampa Bay Rays", "side": "no", "ask": 0.48, "cost": 0.48 + fee_per_contract(0.48),
                "prob": 0.533, "ev": 0.533 - 0.48 - fee_per_contract(0.48), "n_books": 9,
                "matchup": "Milwaukee Brewers @ Tampa Bay Rays", "start": start, "fee_mult": 1.0,
                "research": {"side": {"mentions": 2, "inj": 0}}}
        alert("SAMPLE (not a real play)\nPAPER PLAY\n" + fmt_signal(fake, 32, cfg["min_edge"]))
        alert("SAMPLE (not a real result)\nPAPER LOSS REVIEW\nLos Angeles Lakers lost (Los Angeles Lakers @ "
              "Boston Celtics).\nWhy: price moved -0.05 against us before start")
    elif a.cmd == "test-alert":
        have = [n for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if os.environ.get(n)]
        print(f"Telegram secrets found: {', '.join(have) or 'none'}")
        if len(have) < 2:
            print("Add both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as repo secrets, then run this again.")
        else:
            tok = os.environ["TELEGRAM_BOT_TOKEN"].strip()
            chat = os.environ["TELEGRAM_CHAT_ID"].strip()
            try:   # getMe: which bot does this token belong to? (public info; no secrets printed)
                me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
                if me.get("ok"):
                    print(f"Token belongs to bot: @{me['result'].get('username')} "
                          f"({me['result'].get('first_name')})")
                else:
                    print(f"Token check failed: {me.get('description')}")
            except Exception:
                print("Token check failed: network error")
            raw = os.environ["TELEGRAM_CHAT_ID"]
            notes = []
            if raw != chat:
                notes.append("has spaces/newlines around it")
            if not chat.lstrip("-").isdigit():
                notes.append("is not just digits (remove quotes or words)")
            if chat.lstrip("-") == tok.split(":")[0]:
                notes.append("is the BOT's id (the number at the start of the token), not your chat id")
            try:   # which chats have messaged this bot? show only the last 3 digits (log is public)
                ups = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10).json()
                chats = {}
                for u in ups.get("result", []):
                    c = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")
                    if c:
                        chats[str(c["id"])] = c.get("first_name") or c.get("title") or c.get("type")
                ids_seen = {str(u.get("update_id")) for u in ups.get("result", [])}
                if chat in chats:
                    print("Chat id matches a chat that messaged this bot.")
                elif chats:
                    for cid, who in chats.items():
                        print(f"Chats that messaged this bot: '{who}' -> chat id is {len(cid.lstrip('-'))} "
                              f"digits, ending in ...{cid[-3:]}")
                    if chat in ids_seen:
                        print("  PROBLEM: your saved number is an update_id, not a chat id.")
                    print("  Put that chat id (from \"chat\":{\"id\": ...} on the getUpdates page) in "
                          "TELEGRAM_CHAT_ID.")
                else:
                    print("No recent messages to this bot found. Send it a message in Telegram, then re-run.")
            except Exception:
                print("Couldn't check recent chats (network error).")
            print(f"Chat id check: {len(chat.lstrip('-'))} digits"
                  + (", negative (group/channel)" if chat.startswith("-") else "")
                  + (" | PROBLEM: " + "; ".join(notes) if notes else " | format looks OK"))
        alert(f"Test alert from your Kalshi bot. If you can read this, Telegram alerts work. "
              f"Sent {hst(now_utc())}. Site: https://seed808.github.io/kalshi-bot/")


def discover(q):
    """Find Kalshi series + Odds API sport keys for new sports. Uses NO odds credits."""
    import re as _re
    kws = [k.strip().lower() for k in q.split(",") if k.strip()]
    series = http_get(f"{KALSHI}/series", {"category": "Sports"}).json().get("series", [])
    if kws == ["h2h"]:   # every head-to-head winner series: games, matches, fights, bouts
        pat = _re.compile(r"(GAME|MATCH|FIGHT|BOUT)$")
        found = [s for s in series if pat.search(s.get("ticker", ""))]
        hit = lambda s: True
    else:
        hit = lambda s: any(k in s.lower() for k in kws)
        found = [s for s in series if hit(f'{s.get("ticker", "")} {s.get("title", "")}')]
    print(f"== Kalshi Sports series matching {kws}: {len(found)}")
    for s in found:
        print(f'  {s.get("ticker"):<28} {s.get("title")}')
    for s in found:
        ms = scanner.kalshi_markets({"series_ticker": s["ticker"], "status": "open"}, max_pages=1)
        evs = {}
        for m in ms:
            evs.setdefault(m["event_ticker"], []).append(m)
        print(f'\n== {s["ticker"]} ({s.get("title")}): {len(ms)} open markets, {len(evs)} events; sample:')
        if not ms:
            continue
        for et, mk in list(evs.items())[:(2 if kws == ["h2h"] else 4)]:
            print(f"  {et} ({len(mk)} mkts) title={mk[0].get('title')!r}")
            for m in mk[:3]:
                print(f"    {m['ticker']} yes_sub={m.get('yes_sub_title')!r} "
                      f"exp_end={m.get('expected_expiration_time')} close={m.get('close_time')} "
                      f"bid/ask={m.get('yes_bid_dollars')}/{m.get('yes_ask_dollars')}")
        if ms:
            print(f"  rules: {(ms[0].get('rules_primary') or '')[:400]}")
    key = os.environ.get("ODDS_API_KEY")
    if key:   # /v4/sports does not count against the Odds API quota
        sp = http_get(f"https://api.the-odds-api.com/v4/sports", {"apiKey": key}).json()
        print(f"\n== Odds API sport keys {'(all in season)' if kws == ['h2h'] else f'matching {kws}'}:")
        for s in sp:
            if kws == ["h2h"] or hit(f'{s.get("key")} {s.get("title")} {s.get("group")}'):
                print(f'  {s.get("key"):<40} {s.get("title")} | active={s.get("active")} '
                      f'| outrights={s.get("has_outrights")}')


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
