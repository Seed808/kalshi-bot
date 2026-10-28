"""STEP 5 — Postmortem agents + learning.

After every losing paper trade, five checks run:
  1. stale_odds          sportsbook odds were old when the trade was taken
  2. negative_clv        Kalshi price moved AGAINST the trade before game start
  3. thin_edge           edge was small (< 4c) — likely noise
  4. research_conflict   news/social flagged injuries or negative sentiment for our side
  5. overconfident_range model/consensus has been overconfident in this probability range
If none fire, the loss is logged as variance.

`learn` then looks at recent settled trades (wins AND losses, so it doesn't overreact
to variance) and tightens settings when a flagged pattern is actually losing money.
It only ever tightens; it never loosens. Every change is saved to learned_overrides.json
and logged.
"""
import json

from common import now_utc, load_overrides, save_overrides


def _won(r):
    return r["result"] == r["side"]


def _stats(rows):
    n = len(rows)
    if not n:
        return None
    wins = sum(1 for r in rows if _won(r))
    cost = sum(r["price"] * r["contracts"] + r["fee"] for r in rows)
    pnl = sum(r["pnl"] or 0 for r in rows)
    return {"n": n, "actual": wins / n, "expected": sum(r["prob"] for r in rows) / n,
            "roi": pnl / cost if cost else 0.0}


def research_conflict(r):
    try:
        res = json.loads(r["research"] or "{}")
    except (TypeError, ValueError):
        return False
    side, opp = res.get("side") or {}, res.get("opp") or {}
    return side.get("inj", 0) >= 1 or (side.get("sent", 0) - opp.get("sent", 0)) < -0.1


def _is_stale(r, cfg):
    return r["staleness_min"] is not None and r["staleness_min"] > 0.5 * cfg["max_odds_staleness_min"]


def run_postmortems(cfg, store):
    out = []
    history = store.settled_trades()
    for r in store.q("SELECT * FROM trades WHERE status='settled' AND postmortem_done=0"):
        if _won(r):
            store.x("UPDATE trades SET postmortem_done=1 WHERE id=?", (r["id"],))
            continue
        if r["pick_type"]:   # pick mode: no edge to diagnose, just say what happened
            if r["prob"] >= 0.5:
                verdict = (f"{r['team']} was the favorite ({r['prob']:.0%} per sportsbooks) and lost. "
                           f"Favorites at that price lose about {1 - r['prob']:.0%} of the time.")
            else:
                verdict = (f"Underdog pick ({r['prob']:.0%} per sportsbooks). Underdogs at that price "
                           f"lose about {1 - r['prob']:.0%} of the time.")
            store.x("INSERT INTO postmortems(trade_id,ts,flags,verdict) VALUES(?,?,?,?)",
                    (r["id"], now_utc().isoformat(), json.dumps(["pick"]), verdict))
            store.x("UPDATE trades SET postmortem_done=1 WHERE id=?", (r["id"],))
            out.append((r, ["pick"], verdict))
            continue
        flags, notes = [], []
        if _is_stale(r, cfg):
            flags.append("stale_odds")
            notes.append(f"odds were ~{r['staleness_min']:.0f} min old at entry")
        if r["clv"] is not None and r["clv"] < -0.02:
            flags.append("negative_clv")
            notes.append(f"price moved {r['clv']:+.2f} against us before start")
        if r["ev"] < 0.04:
            flags.append("thin_edge")
            notes.append(f"edge only {r['ev']:.3f}/contract")
        if research_conflict(r):
            flags.append("research_conflict")
            notes.append("research flagged injuries/negative sentiment on our side")
        bucket = [h for h in history if abs(h["prob"] - r["prob"]) <= 0.10]
        st = _stats(bucket)
        if st and st["n"] >= 20 and st["actual"] < st["expected"] - 0.05:
            flags.append("overconfident_range")
            notes.append(f"in this range we win {st['actual']:.0%} vs {st['expected']:.0%} expected "
                         f"(n={st['n']})")
        verdict = "; ".join(notes) if notes else \
            f"variance — model gave this side a {1 - r['prob']:.0%} chance to lose"
        store.x("INSERT INTO postmortems(trade_id,ts,flags,verdict) VALUES(?,?,?,?)",
                (r["id"], now_utc().isoformat(), json.dumps(flags), verdict))
        store.x("UPDATE trades SET postmortem_done=1 WHERE id=?", (r["id"],))
        out.append((r, flags, verdict))
    return out


def learn(cfg, store):
    rows = store.settled_trades(cfg["learn_window"])
    if len(rows) < 15:
        return []
    ov = load_overrides(cfg)
    ov.setdefault("series_min_edge", {})
    ov.setdefault("blocked_series", [])
    changes = []

    stale = [r for r in rows if _is_stale(r, cfg)]
    st = _stats(stale)
    if st and st["n"] >= 8 and st["actual"] < st["expected"] - 0.10:
        new = max(5, round(cfg["max_odds_staleness_min"] * 0.75))
        if new < cfg["max_odds_staleness_min"]:
            ov["max_odds_staleness_min"] = new
            changes.append(f"max_odds_staleness_min -> {new} (stale-odds trades won "
                           f"{st['actual']:.0%} vs {st['expected']:.0%} expected, n={st['n']})")

    for series in sorted({r["series"] for r in rows}):
        thin = [r for r in rows if r["series"] == series and r["ev"] < 0.04]
        st = _stats(thin)
        if st and st["n"] >= 10 and st["roi"] < 0:
            cur = cfg["series_min_edge"].get(series, cfg["min_edge"])
            new = min(0.08, round(cur + 0.005, 4))
            if new > cur:
                ov["series_min_edge"][series] = new
                changes.append(f"{series} min_edge -> {new} (thin-edge ROI {st['roi']:.1%}, n={st['n']})")
        sr = _stats([r for r in rows if r["series"] == series])
        if sr and sr["n"] >= 20 and sr["roi"] < -0.15 and series not in cfg["blocked_series"]:
            ov["blocked_series"] = sorted(set(ov["blocked_series"]) | {series})
            changes.append(f"BLOCKED {series} (ROI {sr['roi']:.1%} over {sr['n']} trades) — "
                           f"remove from learned_overrides.json to re-enable")

    conf = [r for r in rows if research_conflict(r)]
    st = _stats(conf)
    if st and st["n"] >= 8 and st["actual"] < st["expected"] - 0.10 and cfg["injury_veto_count"] > 1:
        ov["injury_veto_count"] = cfg["injury_veto_count"] - 1
        changes.append(f"injury_veto_count -> {ov['injury_veto_count']} (research-conflict trades won "
                       f"{st['actual']:.0%} vs {st['expected']:.0%}, n={st['n']})")

    allst = _stats(rows)
    if allst["n"] >= 30 and allst["actual"] - allst["expected"] < -0.08:
        new = min(0.08, round(cfg["min_edge"] + 0.005, 4))
        if new > cfg["min_edge"]:
            ov["min_edge"] = new
            changes.append(f"min_edge -> {new} (overall win rate {allst['actual']:.0%} vs "
                           f"{allst['expected']:.0%} expected, n={allst['n']})")

    if changes:
        ov.setdefault("history", []).append({"ts": now_utc().isoformat(), "changes": changes})
        save_overrides(cfg, ov)
        for c in changes:
            store.x("INSERT INTO learn_log(ts,change) VALUES(?,?)", (now_utc().isoformat(), c))
    return changes
