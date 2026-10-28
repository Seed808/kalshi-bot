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

from common import fnum, fee_per_contract

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
    Returns (side, market, price, size) for the cheaper one, or None if neither has a price."""
    own = next(m for m in game["markets"] if game["mapping"][m["ticker"]] == team)
    opp = next(m for m in game["markets"] if m is not own)
    yes_ask = fnum(own.get("yes_ask_dollars"))
    no_ask = fnum(opp.get("no_ask_dollars")) or (
        round(1 - fnum(opp.get("yes_bid_dollars")), 4) if fnum(opp.get("yes_bid_dollars")) > 0 else 0.0)
    routes = []
    if 0 < yes_ask < 1:
        routes.append(("yes", own, yes_ask, fnum(own.get("yes_ask_size_fp"))))
    if 0 < no_ask < 1:
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
        "pick_type": kind,
    }, None


def fmt_pick(p, n=None):
    """One plain-language block per pick, for Telegram and the run log."""
    label = p["market"].get("yes_sub_title") or p["team"]
    how = (f'buy YES on "{label}"' if p["side"] == "yes"
           else f'buy NO on "{label}" (same as backing {p["team"]})')
    line = (f'{p["team"]} to beat {p["opponent"]}\n'
            f'{PICK_LABEL[p["pick_type"]]}. Sportsbooks give {p["team"]} {p["prob"] * 100:.0f}%.\n'
            f'Paper: {how} at {p["ask"] * 100:.0f}¢')
    if n:
        line += f', {n} contracts (${n * p["cost"]:,.2f}). Pays ${n:,.2f} if {p["team"]} wins.'
    return line
