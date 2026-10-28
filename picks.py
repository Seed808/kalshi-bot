"""PICK MODE: the bot picks winners for fun. No price edge is required.

THE RULE
    Back the side the sportsbooks give the higher chance to win (the favorite).

Two twists, both optional in config.json:
    pick_upset_rate        With this chance (default 0.25) take the underdog instead, so it isn't
                           all chalk. The coin is seeded by the game's ticker, so the same game
                           always gets the same pick, run after run.
    pick_injury_override   If recent news has at least `injury_veto_count` injury mentions for
                           the favorite (and more than for the underdog), take the underdog.

Each pick is a fixed-dollar PAPER bet (pick_stake_dollars). No real orders, ever.
"""
import hashlib
import random

from common import fnum, fee_per_contract, DEFAULTS


def sport_label(series):
    return DEFAULTS["sport_labels"].get(series, series)

PICK_LABEL = {
    "favorite": "Favorite",
    "upset": "Underdog (upset coin)",
    "injury": "Underdog (injury news on the favorite)",
}


def upset_coin(event_ticker, rate):
    """Seeded coin flip: True means 'take the underdog'. Same ticker -> same answer every run."""
    seed = int(hashlib.sha256(event_ticker.encode()).hexdigest()[:12], 16)
    return random.Random(seed).random() < rate


def choose_side(game, research, cfg):
    """THE PICK RULE. Returns (team_to_back, pick_type, favorite, underdog)."""
    fair = game["oe"]["fair"]                       # sportsbook consensus: {team: win probability}
    a, b = list(game["mapping"].values())
    favorite, underdog = (a, b) if fair[a] >= fair[b] else (b, a)

    if cfg["pick_injury_override"]:
        fav_inj = (research.get(favorite) or {}).get("inj", 0)
        dog_inj = (research.get(underdog) or {}).get("inj", 0)
        if fav_inj >= cfg["injury_veto_count"] and fav_inj > dog_inj:
            return underdog, "injury", favorite, underdog

    if upset_coin(game["event_ticker"], cfg["pick_upset_rate"]):
        return underdog, "upset", favorite, underdog

    return favorite, "favorite", favorite, underdog


def cheapest_route(game, team):
    """A team can be backed on Kalshi two ways: YES on its own market, or NO on the opponent's.
    Returns (side, market, price, size) for the cheaper one, or None if neither has a price.
    Soccer can end in a tie, so there only YES on the team itself means "this team wins"."""
    own = next(m for m in game["markets"] if game["mapping"][m["ticker"]] == team)
    opp = next(m for m in game["markets"] if m is not own)
    yes_ask = fnum(own.get("yes_ask_dollars"))
    no_ask = fnum(opp.get("no_ask_dollars")) or (
        round(1 - fnum(opp.get("yes_bid_dollars")), 4) if fnum(opp.get("yes_bid_dollars")) > 0 else 0.0)
    routes = []
    if 0 < yes_ask < 1:
        routes.append(("yes", own, yes_ask, fnum(own.get("yes_ask_size_fp"))))
    if 0 < no_ask < 1 and not game.get("three_way"):
        routes.append(("no", opp, no_ask, fnum(opp.get("yes_bid_size_fp"))))
    return min(routes, key=lambda r: r[2]) if routes else None


def make_pick(game, research, cfg):
    """Returns (pick, None) or (None, reason). `pick` uses the same fields the trade log expects."""
    team, kind, favorite, underdog = choose_side(game, research, cfg)
    route = cheapest_route(game, team)
    if route is None:
        return None, "no Kalshi price to buy"
    side, market, price, size = route
    prob = game["oe"]["fair"][team]
    cost = price + fee_per_contract(price, game["fee_mult"])
    other = underdog if team == favorite else favorite
    return {
        "event_ticker": game["event_ticker"], "series": game["series"], "matchup": game["matchup"],
        "start": game["start"], "market": market, "team": team, "opponent": other, "side": side,
        "ask": price, "cost": cost, "size": size, "prob": prob, "consensus": prob, "model_prob": None,
        "ev": prob - cost, "n_books": game["oe"]["n_books"], "book_std": game["oe"]["book_std"],
        "staleness": game["oe"]["staleness_min"], "fee_mult": game["fee_mult"],
        "research": {"side": research.get(team) or {}, "opp": research.get(other) or {}},
        "pick_type": kind, "source": game.get("source", "sportsbooks"),
    }, None


def fmt_pick(p, n=None):
    """One plain-language block per pick, for Telegram and the run log."""
    label = p["market"].get("yes_sub_title") or p["team"]
    how = (f'buy YES on "{label}"' if p["side"] == "yes"
           else f'buy NO on "{label}" (same as backing {p["team"]})')
    who = "Sportsbooks give" if p.get("source", "sportsbooks") == "sportsbooks" else "Kalshi prices give"
    line = (f'[{sport_label(p["series"])}] {p["team"]} to beat {p["opponent"]}\n'
            f'{PICK_LABEL[p["pick_type"]]}. {who} {p["team"]} {p["prob"] * 100:.0f}%.\n'
            f'Paper: {how} at {p["ask"] * 100:.0f}¢')
    if n:
        line += f', {n} contracts (${n * p["cost"]:,.2f}). Pays ${n:,.2f} if {p["team"]} wins.'
    return line


# ================================================================ THE SLATE (which picks get in)
#   1. Floor: a pick needs at least `pick_min_win_chance` (55%) to win, unless it's a CLEAR VALUE
#      UNDERDOG: priced by sportsbooks and returning at least `pick_value_underdog_min_return` (8%)
#      per dollar on average (win chance / cost per contract incl. fee - 1).
#   2. Order: highest win chance first. Up to `pick_upset_rate` of the open slots (4 of 15) are
#      held for value underdogs from the coin flip, so the slate isn't pure chalk.
#   3. Caps: `max_open_trades` open and `max_trades_per_day` new picks a day (risk.check_pick).

def value_per_dollar(prob, cost):
    return prob / cost - 1 if cost > 0 else -1.0


def qualifies(prob, cost, from_books, is_dog, cfg):
    """Returns None if the pick may go on the slate, else a plain reason why not."""
    if prob >= cfg["pick_min_win_chance"]:
        return None
    if is_dog and from_books and value_per_dollar(prob, cost) >= cfg["pick_value_underdog_min_return"]:
        return None
    if is_dog:
        return "the upset coin picked the underdog, but it isn't a clear value bet"
    return f'under {cfg["pick_min_win_chance"] * 100:.0f}% win chance, too close to call'


def slate_order(items, room, open_dogs, cfg):
    """items: dicts with prob, value, is_dog, from_books. Returns them in the order to open:
    favorites by win chance, with up to the underdog share of slots held for value underdogs."""
    favs = sorted([x for x in items if not x["is_dog"]], key=lambda x: (-x["prob"], not x["from_books"]))
    dogs = sorted([x for x in items if x["is_dog"]], key=lambda x: -x["value"])
    dog_slots = max(0, round(cfg["max_open_trades"] * cfg["pick_upset_rate"]) - open_dogs)
    take_dogs = dogs[:min(dog_slots, max(0, room))]
    take_favs = favs[:max(0, room - len(take_dogs))]
    return take_favs + take_dogs + favs[len(take_favs):] + dogs[len(take_dogs):]


def trade_item(r):
    """An open trade row as a slate item."""
    cost = r["price"] + (r["fee"] / r["contracts"] if r["contracts"] else 0)
    is_dog = (r["pick_type"] or "") in ("upset", "injury")
    return {"row": r, "prob": r["prob"], "cost": cost, "value": value_per_dollar(r["prob"], cost),
            "is_dog": is_dog, "from_books": bool(r["n_books"])}


def trim_slate(store, cfg, now, parse_ts):
    """Void open picks (games not started yet) that break the slate rules or don't fit under the
    open-pick cap. Voided picks don't count in the record or toward the daily limit."""
    rows = store.open_trades()
    started = [r for r in rows if parse_ts(r["start_ts"]) and parse_ts(r["start_ts"]) <= now]
    started_ids = {r["id"] for r in started}
    waiting = [trade_item(r) for r in rows if r["id"] not in started_ids]
    voided = []
    keep = []
    for x in waiting:
        why = qualifies(x["prob"], x["cost"], x["from_books"], x["is_dog"], cfg)
        (voided.append((x["row"], why)) if why else keep.append(x))
    room = cfg["max_open_trades"] - len(started)
    open_dogs = sum(1 for r in started if (r["pick_type"] or "") in ("upset", "injury"))
    ordered = slate_order(keep, room, open_dogs, cfg)
    for x in ordered[max(0, room):]:
        voided.append((x["row"], f'slate trimmed to the best {cfg["max_open_trades"]}'))
    for r, why in voided:
        store.x("UPDATE trades SET status='void', void_reason=? WHERE id=?", (why, r["id"]))
    return voided
