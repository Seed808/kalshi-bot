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
            flagged.append({"ticker": m["ticker"], "event_ticker": m.get("event_ticker", ""),
                            "title": m.get("title") or "", "option": m.get("yes_sub_title") or "",
                            "vol24": vol, "bid": yb, "ask": ya, "last": last, "prev": prev,
                            "move": move, "spread": (ya - yb) if ya > 0 and yb > 0 else 0.0,
                            "flags": flags, "resolves": rt})
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


def resolve_sport_keys(pattern, api_key, store):
    """Odds API keys to query for one Kalshi series. None -> [] (no sportsbook feed for it).
    'tennis_atp_*' -> every active key with that prefix. A plain key is used only if it's in
    season, so off-season sports never spend a credit. /v4/sports itself is free; cached 6h."""
    if not pattern:
        return []
    cached = store.get_odds_cache("__sports__")
    if cached and cached[0] < 360:
        sports = cached[1]
    else:
        sports = http_get(f"{ODDS}/sports", {"apiKey": api_key}).json()
        store.put_odds_cache("__sports__", sports)
    active = {s.get("key") for s in sports if s.get("active") and not s.get("has_outrights")}
    if not pattern.endswith("*"):
        return [pattern] if (pattern in active or not sports) else []
    pre = pattern[:-1]
    return sorted(k for k in active if k and k.startswith(pre))


def odds_consensus(sport_key, cfg, api_key, store):
    raw, note = fetch_odds_raw(sport_key, cfg, api_key, store)
    if raw is None:
        return [], note
    now = now_utc()
    events = []
    for ev in json.loads(json.dumps(raw)):
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:   # e.g. tennis: take the two names from the first 2-way h2h line
            for bk in ev.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    names = [o.get("name") for o in mkt.get("outcomes", [])]
                    if mkt.get("key") == "h2h" and len(names) == 2 and all(names):
                        home, away = names
                        break
                if home and away:
                    break
            if not home or not away:
                continue
            ev["home_team"], ev["away_team"] = home, away
        probs, stale = [], []   # per book: (p_home, p_away, p_draw) with the margin removed
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                prices = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                if home not in prices or away not in prices:
                    continue
                names = [home, away] + (["Draw"] if "Draw" in prices else [])
                if len(prices) != len(names):
                    continue  # malformed
                inv = [1 / american_to_decimal(prices[n]) for n in names]
                tot = sum(inv)
                probs.append((inv[0] / tot, inv[1] / tot, inv[2] / tot if len(inv) == 3 else 0.0))
                lu = parse_ts(mkt.get("last_update") or bk.get("last_update"))
                if lu:
                    stale.append((now - lu).total_seconds() / 60)
        if len(probs) >= cfg["min_books"]:
            ph = sum(p[0] for p in probs) / len(probs)
            pa = sum(p[1] for p in probs) / len(probs)
            pd = sum(p[2] for p in probs) / len(probs)
            ev["fair"] = {home: ph, away: pa}
            if pd:
                ev["fair"]["Draw"] = pd
            ev["n_books"] = len(probs)
            ev["book_std"] = statistics.pstdev([p[0] for p in probs])
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


def is_tie_market(m):
    return (m.get("yes_sub_title") or "").strip().lower() in ("tie", "draw") or m["ticker"].endswith("-TIE")


def kalshi_only_game(series, et, mk, mk_all, three_way, cfg, now, fmult):
    """Build a game from Kalshi's own prices when no sportsbook has it. Returns (game, None),
    (None, reason) when it's in the window but unusable, or (None, None) when outside the window."""
    end = game_time_ref(mk)
    if not end:
        return None, None
    gh = cfg["game_hours"]
    hours = gh.get(series) or (gh.get("soccer", 2.0) if three_way else gh.get("default", 3.0))
    start = end - timedelta(hours=hours)   # estimated; Kalshi only gives the expected end
    if start <= now or start > now + timedelta(hours=cfg["max_hours_ahead"]):
        return None, None
    mids = []
    for m in mk:
        yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        if not (0 < yb < ya < 1) or ya - yb > cfg["pick_kalshi_max_spread"]:
            return None, "no sportsbook line, and Kalshi's prices are too thin to tell who's favored"
        mids.append((yb + ya) / 2)
    tie = 0.0
    if three_way:
        t = next((m for m in mk_all if is_tie_market(m)), None)
        tb, ta = (fnum(t.get("yes_bid_dollars")), fnum(t.get("yes_ask_dollars"))) if t else (0, 0)
        tie = (tb + ta) / 2 if 0 < tb < ta < 1 else max(0.0, 1 - sum(mids))
    total = sum(mids) + tie
    names = [m.get("yes_sub_title") or m["ticker"] for m in mk]
    if names[0] == names[1]:
        return None, "couldn't tell the two sides apart"
    fair = {names[0]: mids[0] / total, names[1]: mids[1] / total}
    oe = {"id": et, "home_team": names[1], "away_team": names[0], "commence_time": start.isoformat(),
          "fair": fair, "n_books": 0, "book_std": 0.0, "staleness_min": None}
    return {"series": series, "event_ticker": et, "markets": mk,
            "mapping": {mk[0]["ticker"]: names[0], mk[1]["ticker"]: names[1]}, "oe": oe,
            "start": start, "fee_mult": fmult, "three_way": three_way, "source": "kalshi",
            "matchup": f"{names[0]} vs {names[1]}"}, None


def sports_games(cfg, api_key, store, verbose=False, skipped=None):
    """Return (games, all_markets_by_ticker). If `skipped` is a list, Kalshi games in the pick
    window that couldn't be used are appended to it as {series, names, ends, reason}."""
    now = now_utc()
    games, all_markets = [], {}
    window_end = now + timedelta(hours=cfg["max_hours_ahead"] + 4)   # games end a few hours after start

    def note_skip(series, mk, reason):
        if skipped is None:
            return
        end = game_time_ref(mk)
        if end and now < end <= window_end:
            skipped.append({"series": series, "names": [m.get("yes_sub_title") or "" for m in mk],
                            "ends": end.isoformat(), "reason": reason})
    pick_mode = cfg.get("mode") == "picks"
    fallback = pick_mode and cfg.get("pick_kalshi_fallback", False)
    soccer = set(cfg.get("soccer_series", []))

    # 1) Kalshi markets for every series (free)
    loaded = []
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
        ends = [e for e in (parse_ts(m.get("expected_expiration_time")) for m in ms) if e and e > now]
        loaded.append((min(ends) if ends else now + timedelta(days=3650), series, sport, ms))

    # 2) sports with the soonest games go first, so the free daily odds credits go to them
    loaded.sort(key=lambda t: t[0])
    for _soonest, series, sport, ms in loaded:
        odds_events, notes = [], []
        horizon = now + timedelta(hours=cfg["odds_spend_horizon_hours"])
        soon = [e for e in (parse_ts(m.get("expected_expiration_time")) for m in ms) if e]
        if not sport:
            notes.append("no sportsbook feed for this sport")
        elif not any(now < e <= horizon for e in soon):
            notes.append(f"no games ending within {cfg['odds_spend_horizon_hours']}h, no odds bought")
        else:
            try:
                keys = resolve_sport_keys(sport, api_key, store)
            except Exception as e:
                keys = []
                notes.append(f"sports-list error: {e}")
            if not keys and not notes:
                notes.append(f"{sport} isn't in season at the sportsbooks")
            for key in keys:
                try:
                    evs, note = odds_consensus(key, cfg, api_key, store)
                except Exception as e:
                    print(f"[{key}] Odds API error: {e}")
                    continue
                odds_events.extend(evs)
                notes.append(f"{key.split('_', 2)[-1] + ':' if '*' in sport else ''}{note}")
        remaining = "; ".join(notes)
        no_odds_reason = ("no sportsbook odds for this sport right now" if not odds_events
                          else "not found at the sportsbooks (or too few books price it)")
        fmult = fee_multiplier(series)
        by_event = {}
        for m in ms:
            by_event.setdefault(m["event_ticker"], []).append(m)
        matched = unmatched = dup = kal = 0
        claims = {}   # odds event id -> list of (event_ticker, team markets, mapping, oe, three_way)
        for et, mk_all in by_event.items():
            mk = [m for m in mk_all if not is_tie_market(m)]   # soccer: drop the "Tie" market
            three_way = len(mk) != len(mk_all) or series in soccer
            if len(mk) != 2 or (three_way and not pick_mode):
                continue   # edge mode handles two-outcome games only
            oe, mapping, _gap = match_event(et, mk, odds_events, cfg["match_threshold"])
            if not oe:
                unmatched += 1
                if fallback:   # no sportsbook line: let Kalshi's own prices say who's favored
                    g, why = kalshi_only_game(series, et, mk, mk_all, three_way, cfg, now, fmult)
                    if g:
                        games.append(g)
                        kal += 1
                    elif why:
                        note_skip(series, mk, why)
                    continue
                note_skip(series, mk, no_odds_reason)
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
            claims.setdefault(oe.get("id") or id(oe), []).append((et, mk, mapping, oe, three_way))
        for oid, cl in claims.items():
            if len(cl) > 1:   # two Kalshi games claim one sportsbook game -> ambiguous, skip all
                dup += len(cl)
                for c in cl:
                    note_skip(series, c[1], "couldn't tell which sportsbook game this is")
                if verbose:
                    print(f"  ambiguous: {', '.join(c[0] for c in cl)} all match one odds game — skipped")
                continue
            et, mk, mapping, oe, three_way = cl[0]
            start = parse_ts(oe.get("commence_time"))
            if not start or start <= now or start > now + timedelta(hours=cfg["max_hours_ahead"]):
                continue
            matched += 1
            games.append({"series": series, "event_ticker": et, "markets": mk, "mapping": mapping,
                          "oe": oe, "start": start, "fee_mult": fmult, "three_way": three_way,
                          "source": "sportsbooks",
                          "matchup": (f'{oe["home_team"]} vs {oe["away_team"]}'
                                      if series in cfg.get("individual_series", [])
                                      else f'{oe["away_team"]} @ {oe["home_team"]}')})
        if verbose:
            print(f"[{series}] {len(ms)} mkts | {len(odds_events)} odds events | matched {matched} "
                  f"| kalshi-only {kal} | unmatched {unmatched - kal} | ambiguous {dup} | odds: {remaining}")
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


# ---------------------------------------------------------------- plain-English titles

def enrich_anomalies(flagged, store, limit=8):
    """Add each flagged market's event title (e.g. 'Memphis at Utah: Rebounds') from Kalshi's
    public /events endpoint, cached a day, so the website never shows raw ticker codes."""
    for a in flagged[:limit]:
        et = a.get("event_ticker")
        if not et:
            continue
        key = f"evtitle:{et}"
        cached = store.get_meta(key)
        if cached is None:
            try:
                ev = http_get(f"{KALSHI}/events/{et}").json().get("event", {})
                cached = json.dumps({"title": ev.get("title") or "", "sub": ev.get("sub_title") or ""})
            except Exception:
                cached = json.dumps({"title": "", "sub": ""})
            store.set_meta(key, cached)
        d = json.loads(cached)
        a["event_title"], a["event_sub"] = d.get("title", ""), d.get("sub", "")
        a["series_ticker"] = et.split("-")[0]
    return flagged
