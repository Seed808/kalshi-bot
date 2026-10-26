"""STEP 1 — Scan agent.

(a) anomaly_scan: pages through ALL open Kalshi markets, filters by 24h volume and
    time to resolution, flags big 24h price moves and wide spreads.
(b) sports_games: pulls open game-winner markets per configured series, matches each
    game to de-vigged sportsbook consensus odds, keeps pregame games in the window.
"""
import json
import math
import statistics
from datetime import timedelta

from common import (KALSHI, ODDS, http_get, fnum, parse_ts, now_utc, american_to_decimal,
                    devig_two_way, name_score, ticker_date)


# ---------------------------------------------------------------- Kalshi

def kalshi_markets(params, max_pages=50):
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        p = dict(params, limit=1000)
        if cursor:
            p["cursor"] = cursor
        data = http_get(f"{KALSHI}/markets", p).json()
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        pages += 1
        if not cursor:
            break
    return out


def markets_by_tickers(tickers):
    out = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        data = http_get(f"{KALSHI}/markets", {"tickers": ",".join(chunk), "limit": 1000}).json()
        out.extend(data.get("markets", []))
    return out


def fee_multiplier(series_ticker):
    try:
        s = http_get(f"{KALSHI}/series/{series_ticker}").json().get("series", {})
        return fnum(s.get("fee_multiplier"), 1.0) or 1.0
    except Exception:
        return 1.0


def resolution_time(m):
    return parse_ts(m.get("expected_expiration_time")) or parse_ts(m.get("close_time"))


def anomaly_scan(cfg):
    now = now_utc()
    ms = kalshi_markets({"status": "open", "mve_filter": "exclude"}, cfg["scan_max_pages"])
    passed, flagged = 0, []
    for m in ms:
        vol = fnum(m.get("volume_24h_fp"))
        rt = resolution_time(m)
        if vol < cfg["scan_min_volume_24h"] or not rt or rt <= now \
                or rt > now + timedelta(hours=cfg["scan_max_hours"]):
            continue
        passed += 1
        yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        last, prev = fnum(m.get("last_price_dollars")), fnum(m.get("previous_price_dollars"))
        flags = []
        move = last - prev if last > 0 and prev > 0 else 0.0
        if abs(move) >= cfg["scan_move_threshold"]:
            flags.append(f"24h move {move:+.2f}")
        if ya > 0 and yb > 0 and (ya - yb) >= cfg["scan_wide_spread"]:
            flags.append(f"spread {ya - yb:.2f}")
        if flags:
            flagged.append({"ticker": m["ticker"], "title": m.get("title") or m.get("yes_sub_title"),
                            "vol24": vol, "bid": yb, "ask": ya, "last": last, "flags": flags,
                            "resolves": rt})
    flagged.sort(key=lambda x: x["vol24"], reverse=True)
    return {"total": len(ms), "passed": passed, "flagged": flagged}


# ---------------------------------------------------------------- odds

def odds_budget_ok(cfg, store):
    """Spread the free-plan credits evenly over the rest of the month.

    Uses the smaller of: our own monthly budget minus credits logged this month, and the
    Odds API's own x-requests-remaining header minus a reserve. Never overspends.
    """
    now = now_utc()
    used_month, used_today = store.odds_credits_used(now)
    left = cfg["odds_monthly_credit_budget"] - used_month
    rem = store.get_meta("odds_remaining")
    if rem is not None:
        left = min(left, fnum(rem) - cfg["odds_credit_reserve"])
    if left <= 0:
        return False, "monthly free odds credits used up — waiting for reset"
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = max(1, (nxt.date() - now.date()).days)
    daily = min((left + used_today) / days_left, cfg["odds_max_credits_per_day"])
    if used_today >= daily:
        return False, f"daily odds allowance reached ({used_today:.0f}/{daily:.1f})"
    return True, ""


def fetch_odds_raw(sport_key, cfg, api_key, store):
    """Return (raw_events_or_None, note). Reuses cache; spends credits only within budget."""
    cached = store.get_odds_cache(sport_key)
    if cached and cached[0] < cfg["odds_cache_min"]:
        return cached[1], f"cached {cached[0]:.0f}m"
    ok, why = odds_budget_ok(cfg, store)
    if not ok:
        return (cached[1] if cached else None), why
    params = {"apiKey": api_key, "regions": cfg["odds_regions"], "markets": "h2h",
              "oddsFormat": "american"}
    if cfg["bookmakers"]:
        params.pop("regions")
        params["bookmakers"] = ",".join(cfg["bookmakers"])
    r = http_get(f"{ODDS}/sports/{sport_key}/odds", params)
    raw = r.json()
    rem = r.headers.get("x-requests-remaining")
    if rem is not None:
        store.set_meta("odds_remaining", rem)
    cost = fnum(r.headers.get("x-requests-last"), 1.0) if raw else 0.0
    store.log_odds_call(sport_key, cost)
    store.put_odds_cache(sport_key, raw)
    return raw, f"fresh, {cost:.0f} credit(s), {rem} left"


def odds_consensus(sport_key, cfg, api_key, store):
    raw, note = fetch_odds_raw(sport_key, cfg, api_key, store)
    if raw is None:
        return [], note
    now = now_utc()
    events = []
    for ev in json.loads(json.dumps(raw)):
        home, away = ev.get("home_team"), ev.get("away_team")
        probs, stale = [], []
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                if len(prices) != 2 or home not in prices or away not in prices:
                    continue  # skip 3-way / malformed
                ph, _ = devig_two_way(american_to_decimal(prices[home]),
                                      american_to_decimal(prices[away]))
                probs.append(ph)
                lu = parse_ts(mkt.get("last_update") or bk.get("last_update"))
                if lu:
                    stale.append((now - lu).total_seconds() / 60)
        if len(probs) >= cfg["min_books"]:
            ph = sum(probs) / len(probs)
            ev["fair"] = {home: ph, away: 1 - ph}
            ev["n_books"] = len(probs)
            ev["book_std"] = statistics.pstdev(probs)
            ev["staleness_min"] = statistics.median(stale) if stale else None
            events.append(ev)
    return events, note


MAX_GAME_HOURS = 12   # a game's start must fall within this many hours before the market's expected end


def game_time_ref(mk):
    """Kalshi game markets carry expected_expiration_time (~game end). Use it to pin the exact game."""
    exp = [parse_ts(m.get("expected_expiration_time")) for m in mk]
    exp = [e for e in exp if e]
    return min(exp) if exp else None


def time_distance(end_ref, commence):
    """Hours from game start to expected market end, or None if this odds game can't be that game."""
    if not end_ref or not commence:
        return None
    gap = (end_ref - commence).total_seconds() / 3600
    return gap if 0 < gap <= MAX_GAME_HOURS else None


def match_event(event_ticker, mk, odds_events, threshold):
    """Match a Kalshi game (2 markets) to ONE sportsbook game: names must match AND the start time
    must fit the market's own expected end. Prevents pairing back-to-back games of a series."""
    end_ref = game_time_ref(mk)
    if end_ref is None:
        return None, None, None   # can't pin the game in time -> never guess
    best, best_key, best_map, best_gap = None, None, None, None
    for oe in odds_events:
        gap = time_distance(end_ref, parse_ts(oe.get("commence_time")))
        if gap is None:
            continue
        home, away = oe.get("home_team"), oe.get("away_team")
        n0, n1 = mk[0]["yes_sub_title"], mk[1]["yes_sub_title"]
        straight = (name_score(n0, home), name_score(n1, away))
        cross = (name_score(n0, away), name_score(n1, home))
        if min(straight) >= min(cross):
            s, mapping = straight, {mk[0]["ticker"]: home, mk[1]["ticker"]: away}
        else:
            s, mapping = cross, {mk[0]["ticker"]: away, mk[1]["ticker"]: home}
        if min(s) >= threshold:
            key = (-sum(s), gap)          # best names first, then closest in time
            if best_key is None or key < best_key:
                best, best_key, best_map, best_gap = oe, key, mapping, gap
    return best, best_map, best_gap


def sports_games(cfg, api_key, store, verbose=False):
    """Return (games, all_markets_by_ticker)."""
    now = now_utc()
    games, all_markets = [], {}
    for series, sport in cfg["series"].items():
        if series in cfg["blocked_series"]:
            if verbose:
                print(f"[{series}] blocked by learning rules — skipped")
            continue
        try:
            ms = kalshi_markets({"series_ticker": series, "status": "open"})
        except Exception as e:
            print(f"[{series}] Kalshi error: {e}")
            continue
        for m in ms:
            all_markets[m["ticker"]] = m
        if not ms:
            if verbose:
                print(f"[{series}] no open markets")
            continue
        # don't spend an odds credit unless some game in this series resolves soon
        horizon = now + timedelta(hours=cfg["max_hours_ahead"] + 12)
        if not any((resolution_time(m) or horizon + timedelta(days=1)) <= horizon for m in ms):
            if verbose:
                print(f"[{series}] no games in window — no odds credit spent")
            continue
        fmult = fee_multiplier(series)
        try:
            odds_events, remaining = odds_consensus(sport, cfg, api_key, store)
        except Exception as e:
            print(f"[{sport}] Odds API error: {e}")
            continue
        by_event = {}
        for m in ms:
            by_event.setdefault(m["event_ticker"], []).append(m)
        matched = unmatched = dup = 0
        claims = {}   # odds event id -> list of (event_ticker, markets, mapping, oe)
        for et, mk in by_event.items():
            if len(mk) != 2:
                continue
            oe, mapping, _gap = match_event(et, mk, odds_events, cfg["match_threshold"])
            if not oe:
                unmatched += 1
                if verbose and unmatched <= 4:
                    end = game_time_ref(mk)
                    names = [m.get("yes_sub_title") for m in mk]
                    near = sorted(odds_events, key=lambda o: -max(name_score(names[0], o["home_team"]),
                                                                  name_score(names[0], o["away_team"])))[:1]
                    nd = (f'{near[0]["away_team"]} @ {near[0]["home_team"]} starts '
                          f'{near[0]["commence_time"]}') if near else "none"
                    print(f"  unmatched {et}: {names} ends {end.isoformat() if end else 'n/a'} "
                          f"| closest odds: {nd}")
                continue
            claims.setdefault(oe.get("id") or id(oe), []).append((et, mk, mapping, oe))
        for oid, cl in claims.items():
            if len(cl) > 1:   # two Kalshi games claim one sportsbook game -> ambiguous, skip all
                dup += len(cl)
                if verbose:
                    print(f"  ambiguous: {', '.join(c[0] for c in cl)} all match one odds game — skipped")
                continue
            et, mk, mapping, oe = cl[0]
            start = parse_ts(oe.get("commence_time"))
            if not start or start <= now or start > now + timedelta(hours=cfg["max_hours_ahead"]):
                continue
            matched += 1
            games.append({"series": series, "event_ticker": et, "markets": mk, "mapping": mapping,
                          "oe": oe, "start": start, "fee_mult": fmult,
                          "matchup": f'{oe["away_team"]} @ {oe["home_team"]}'})
        if verbose:
            print(f"[{series}] {len(ms)} mkts | {len(odds_events)} odds events | matched {matched} "
                  f"| unmatched {unmatched} | ambiguous {dup} | odds: {remaining}")
    return games, all_markets


# ---------------------------------------------------------------- features

FEATURES = ["mid", "fair", "fair_minus_mid", "book_std", "n_books", "spread", "log_vol24",
            "move24", "hours_to_start", "has_research", "sent_team", "sent_opp", "sent_diff",
            "inj_team", "inj_opp", "mentions_team", "mentions_opp", "staleness_min", "series_idx"]


def opponent(game, team):
    t = list(game["mapping"].values())
    return t[1] if t[0] == team else t[0]


def market_features(game, m, research, series_idx):
    team = game["mapping"][m["ticker"]]
    opp = opponent(game, team)
    oe = game["oe"]
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    mid = (yb + ya) / 2 if yb > 0 and ya > 0 else fnum(m.get("last_price_dollars"))
    last, prev = fnum(m.get("last_price_dollars")), fnum(m.get("previous_price_dollars"))
    rt, ro = research.get(team), research.get(opp)
    fair = oe["fair"][team]
    return {
        "mid": mid, "fair": fair, "fair_minus_mid": fair - mid,
        "book_std": oe["book_std"], "n_books": oe["n_books"],
        "spread": (ya - yb) if ya > 0 and yb > 0 else 0.0,
        "log_vol24": math.log1p(fnum(m.get("volume_24h_fp"))),
        "move24": (last - prev) if last > 0 and prev > 0 else 0.0,
        "hours_to_start": (game["start"] - now_utc()).total_seconds() / 3600,
        "has_research": 1.0 if (rt or ro) else 0.0,
        "sent_team": (rt or {}).get("sent", 0.0), "sent_opp": (ro or {}).get("sent", 0.0),
        "sent_diff": (rt or {}).get("sent", 0.0) - (ro or {}).get("sent", 0.0),
        "inj_team": (rt or {}).get("inj", 0), "inj_opp": (ro or {}).get("inj", 0),
        "mentions_team": (rt or {}).get("mentions", 0), "mentions_opp": (ro or {}).get("mentions", 0),
        "staleness_min": oe["staleness_min"] if oe["staleness_min"] is not None else -1.0,
        "series_idx": series_idx,
    }
