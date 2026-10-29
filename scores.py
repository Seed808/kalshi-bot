"""Final scores for the pick history, from ESPN's free public scoreboards (no key, no credits).

Kalshi's settlement decides whether a pick won or lost. ESPN only supplies the score shown next to
it. A pick whose game can't be found keeps "score not available"; the bot retries a few times.
"""
from datetime import timedelta

from common import http_get, parse_ts, name_score, strip_accents

ESPN = "https://site.api.espn.com/apis/site/v2/sports"

# Kalshi series -> (ESPN sport/league path, extra query params)
ESPN_PATHS = {
    "KXNFLGAME": ("football/nfl", {}),
    "KXNCAAFGAME": ("football/college-football", {"groups": "80", "limit": "500"}),
    "KXCFLGAME": ("football/cfl", {}),
    "KXNBAGAME": ("basketball/nba", {}),
    "KXNCAAMBGAME": ("basketball/mens-college-basketball", {"groups": "50", "limit": "500"}),
    "KXNCAAWBGAME": ("basketball/womens-college-basketball", {"groups": "50", "limit": "500"}),
    "KXWNBAGAME": ("basketball/wnba", {}),
    "KXMLBGAME": ("baseball/mlb", {}),
    "KXNCAABBGAME": ("baseball/college-baseball", {"limit": "500"}),
    "KXNHLGAME": ("hockey/nhl", {}),
    "KXNCAAHGAME": ("hockey/mens-college-hockey", {"limit": "500"}),
    "KXMLSGAME": ("soccer/usa.1", {}),
    "KXNWSLGAME": ("soccer/usa.nwsl", {}),
    "KXEPLGAME": ("soccer/eng.1", {}),
    "KXUCLGAME": ("soccer/uefa.champions", {}),
    "KXLALIGAGAME": ("soccer/esp.1", {}),
    "KXBUNDESLIGAGAME": ("soccer/ger.1", {}),
    "KXSERIEAGAME": ("soccer/ita.1", {}),
    "KXLIGUE1GAME": ("soccer/fra.1", {}),
    "KXLIGAMXGAME": ("soccer/mex.1", {}),
    "KXUFCFIGHT": ("mma/ufc", {}),
    "KXATPMATCH": ("tennis/atp", {}),
    "KXWTAMATCH": ("tennis/wta", {}),
    "KXNCAAWVMATCH": ("volleyball/womens-college-volleyball", {"limit": "500"}),
}
MAX_TRIES = 8   # runs to keep looking for a score before giving up


def _names(c):
    """All the names ESPN gives a competitor (team or athlete)."""
    t = c.get("team") or c.get("athlete") or {}
    out = [t.get(k) for k in ("displayName", "shortDisplayName", "name", "fullName", "shortName", "nickname")]
    if t.get("location") and t.get("name"):
        out.append(f'{t["location"]} {t["name"]}')   # never the city alone: "Los Angeles" is two teams
    return [strip_accents(n) for n in out if n]


def _short(c):
    t = c.get("team") or c.get("athlete") or {}
    return t.get("shortDisplayName") or t.get("shortName") or t.get("displayName") or "?"


def _score(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue") or s.get("value")
    if s in (None, ""):
        return None
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(s)


def _sets(c):
    return [ls.get("value") for ls in (c.get("linescores") or []) if isinstance(ls, dict)]


def competitions(node, status=None):
    """Every head-to-head competition anywhere in an ESPN scoreboard (handles tennis groupings
    and fight cards too): yields (competitors[2], status_dict)."""
    if isinstance(node, dict):
        st = node.get("status") or status
        comps = node.get("competitors")
        if isinstance(comps, list) and len(comps) == 2:
            yield comps, st
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from competitions(v, st)
    elif isinstance(node, list):
        for v in node:
            yield from competitions(v, status)


def _match_score(ours, theirs):
    best = 0.0
    for n in theirs:
        best = max(best, name_score(ours, n), name_score(n, ours))
    return best


def find_result(team, opponent, comps):
    """The competition between `team` and `opponent`, as (team_comp, opp_comp, status) or None."""
    best, best_s = None, 0.0
    for pair, st in comps:
        a, b = pair
        straight = min(_match_score(team, _names(a)), _match_score(opponent, _names(b)))
        cross = min(_match_score(team, _names(b)), _match_score(opponent, _names(a)))
        s, tc, oc = (straight, a, b) if straight >= cross else (cross, b, a)
        if s >= 1.0 and s > best_s:
            best, best_s = (tc, oc, st), s
    return best


def format_score(team_c, opp_c, status, series):
    """'Spurs 118, Jazz 104 (Final)' / 'Osaka 6-4, 7-6 (Final)' / 'Smith won (Final)'."""
    detail = ((status or {}).get("type") or {}).get("shortDetail") or ((status or {}).get("type") or {}).get("detail")
    done = ((status or {}).get("type") or {}).get("completed")
    sets_t, sets_o = _sets(team_c), _sets(opp_c)
    if series in ("KXATPMATCH", "KXWTAMATCH") and sets_t and len(sets_t) == len(sets_o):
        body = f'{_short(team_c)} ' + ", ".join(f"{int(x)}-{int(y)}" for x, y in zip(sets_t, sets_o)
                                                if x is not None and y is not None)
    else:
        st, so = _score(team_c), _score(opp_c)
        if st is not None and so is not None:
            body = f"{_short(team_c)} {st}, {_short(opp_c)} {so}"
        elif team_c.get("winner") or opp_c.get("winner"):
            body = f'{_short(team_c if team_c.get("winner") else opp_c)} won'
        else:
            return None, done
    return f"{body}{f' ({detail})' if detail else ''}", done


def espn_dates(start):
    """ESPN files games under the US Eastern date; check that day and its neighbors."""
    east = start - timedelta(hours=5)
    return [(east + timedelta(days=k)).strftime("%Y%m%d") for k in (0, -1, 1)]


def backfill(store, verbose=False):
    """Fill in final scores for settled picks that don't have one yet."""
    rows = store.q("""SELECT * FROM trades WHERE status='settled' AND score_text IS NULL
                      AND COALESCE(score_tries, 0) < ?""", (MAX_TRIES,))
    cache, found = {}, 0
    for r in rows:
        path = ESPN_PATHS.get(r["series"])
        start = parse_ts(r["start_ts"])
        if not path or not start:
            store.x("UPDATE trades SET score_tries=? WHERE id=?", (MAX_TRIES, r["id"]))
            continue
        opp = ""
        for sep in (" @ ", " vs "):
            if sep in r["matchup"]:
                a, b = r["matchup"].split(sep, 1)
                opp = b if a == r["team"] else a
        hit = None
        for day in espn_dates(start):
            key = (path[0], day)
            if key not in cache:
                try:
                    cache[key] = list(competitions(http_get(f"{ESPN}/{path[0]}/scoreboard",
                                                            dict(path[1], dates=day)).json()))
                except Exception as e:
                    print(f"ESPN {path[0]} {day}: {e}")
                    cache[key] = []
            hit = find_result(r["team"], opp, cache[key])
            if hit:
                break
        text, done = (format_score(*hit, r["series"]) if hit else (None, None))
        if hit and (hit[0].get("winner") or hit[1].get("winner")):
            # safety: ESPN's winner must agree with Kalshi's settlement, or this is the wrong game
            if bool(hit[0].get("winner")) != (r["result"] == r["side"]):
                print(f"  ESPN winner disagrees with Kalshi for {r['matchup']}: score not used")
                text = None
        if text and done is not False:
            store.x("UPDATE trades SET score_text=? WHERE id=?", (text, r["id"]))
            found += 1
        else:
            store.x("UPDATE trades SET score_tries=COALESCE(score_tries,0)+1 WHERE id=?", (r["id"],))
            if verbose:
                print(f"  no ESPN score yet: {r['matchup']} ({r['series']}, {r['start_ts']})")
    if verbose and rows:
        print(f"Scores: {found} of {len(rows)} found on ESPN")
    return found
