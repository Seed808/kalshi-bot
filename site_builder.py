"""Builds the public price-board website (docs/index.html) after every cycle.

GitHub Pages serves the docs/ folder. Static HTML, no JavaScript, no external data calls:
everything shown is computed here from the bot's own database and the cycle's board.
"""
import json
import os
from datetime import timedelta
from html import escape

from common import now_utc, parse_ts, hst, HST, DEFAULTS

SPORT = DEFAULTS["sport_labels"]
OUT_DIR = "docs"


def cents(x):
    return f"{x * 100:.0f}¢"


def pct(x):
    return f"{x * 100:.1f}%"


def signed_cents(x):
    return f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}¢"


def when(iso):
    d = parse_ts(iso)
    return d.astimezone(HST).strftime("%a %-I:%M %p") if d else ""


TENNIS = ("KXATPMATCH", "KXWTAMATCH")


def span_text(minutes):
    m = int(round(abs(minutes)))
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def start_phrase(start_iso, now, estimated, tennis):
    """Same wording the page's JavaScript produces live; always HST."""
    d = parse_ts(start_iso)
    if not d:
        return ""
    t = when(start_iso) + " HST"
    mins = (d - now).total_seconds() / 60
    if mins > 0:
        if tennis:
            return f"Scheduled {t} (in {span_text(mins)}; tennis order of play can slip)."
        if estimated:
            return f"Starts about {t} (in {span_text(mins)}; estimated from Kalshi)."
        return f"Starts {t} (in {span_text(mins)})."
    lead = "Scheduled for" if tennis else ("Started about" if estimated else "Started")
    return f"{lead} {t} ({span_text(mins)} ago), waiting for the result."


def other_side(b, x):
    return next((s for s in b["sides"] if s is not x), x)


def best_side(b):
    sides = [x for x in b["sides"] if x["ev"] is not None and x["ask"]]
    return max(sides, key=lambda x: x["ev"]) if sides else None


def action(b, x):
    """Plain instruction for backing side x, using the cheaper Kalshi route."""
    if x.get("route") == "no on opponent":
        o = other_side(b, x)
        return (f'buy No on {escape(o["name"])} at {cents(x["ask"])}',
                f'Buying No on {escape(o["name"])} is the same bet as backing {escape(x["name"])}, '
                f'and it\'s cheaper right now.')
    return f'buy Yes on {escape(x["name"])} at {cents(x["ask"])}', ""


def role_note(x):
    """Say plainly whether the side is favored, and what backing an underdog means."""
    if x["fair"] < 0.5:
        return (f' {escape(x["name"])} is the underdog: sportsbooks expect them to lose about '
                f'{(1 - x["fair"]) * 100:.0f} times out of 100, so this bet loses more often than it wins. '
                f'The case for it is only that the price is low for that chance.')
    return f' {escape(x["name"])} is the favorite, expected to win about {x["fair"] * 100:.0f} times out of 100.'


def ten_dollar(x):
    """What a $10 paper stake on side x looks like. cost per contract = price + Kalshi fee."""
    cost = x["fair"] - x["ev"]
    n = int(10 // cost) if cost > 0 else 0
    if n < 1:
        return ""
    spend = n * cost
    return (f'<p class="eg">$10 example: {n} contracts cost ${spend:.2f} with fees. If '
            f'{escape(x["name"])} wins, you collect ${n:.2f} (a ${n - spend:.2f} profit). If not, you '
            f'lose the ${spend:.2f}. On average that works out to '
            f'{"+" if x["ev"] >= 0 else "−"}${abs(n * x["ev"]):.2f}.</p>')


SKIP_TEXT = {
    "odds stale": ("Waiting for fresh odds",
                   "The sportsbook odds here are more than 30 minutes old. To stay on the free plan the bot "
                   "only refreshes them a few times a day, and it won't trade on old prices. Prices below "
                   "are from the last refresh."),
    "sportsbooks disagree": ("Skipped: sportsbooks disagree",
                             "The sportsbooks' prices are too far apart to trust a single fair price."),
    "research veto": ("Skipped: injury news",
                      "Recent news mentions an injury for the side that looked cheap, so the bot sits out."),
    "confidence gate": ("Skipped: model and sportsbooks disagree",
                        "The bot's model and the sportsbooks don't both see an edge, so it passes."),
}


def verdict(b, need):
    x = best_side(b)
    if b["signal"]:
        x = next((s for s in b["sides"] if s["name"] == b["signal"]["team"]), x)
        act, note = action(b, x)
        return ("play", f'Paper play: {act}',
                f'Sportsbooks give {escape(x["name"])} a {pct(x["fair"])} chance, so each contract is '
                f'worth about {signed_cents(x["ev"])} more than it costs after fees.{role_note(x)} {note}', x)
    if b["why"] and b["why"] != "no edge":
        head, why = SKIP_TEXT.get(b["why"].split(":")[0], (
            f'Skipped: {escape(b["why"])}',
            'The bot doesn\'t trust the sportsbook price for this one, so it won\'t trade it.'))
        return ("skip", head, why, None)
    if x is None:
        return ("skip", "No Kalshi price yet", "", None)
    act, note = action(b, x)
    if x["ev"] > 0:
        return ("pass", "Pass",
                f'Best option is to {act}, but it\'s only {signed_cents(x["ev"])} per contract on '
                f'average. The bot needs at least {signed_cents(need)}.{role_note(x)} {note}', x)
    return ("pass", "Pass",
            f'Both players cost more on Kalshi than the sportsbooks say they\'re worth. The closest is '
            f'{escape(x["name"])} at {signed_cents(x["ev"])} per contract.'
            if "vs" in b["matchup"] else
            f'Both teams cost more on Kalshi than the sportsbooks say they\'re worth. The closest is '
            f'{escape(x["name"])} at {signed_cents(x["ev"])} per contract.', x)


HOWTO = """<details class="howto" open><summary>How to read this board</summary><ul>
<li>A Kalshi contract pays <b>$1 if your side wins</b> and nothing if it loses. The price in cents is
what one contract costs, so 29¢ means you pay $0.29 to maybe win $1.</li>
<li><b>Sportsbooks' chance</b> is how likely several sportsbooks think that side is to win, with
their built-in margin removed.</li>
<li><b>Avg. profit per contract</b> is the sportsbooks' chance minus the Kalshi price minus Kalshi's fee.
Positive means Kalshi is selling it cheap. Negative means it's overpriced.</li>
<li>The bot only makes a paper play when that number is at least +{need}. Anything smaller is
too close to call.</li></ul></details>"""


def hero(board, cfg):
    need = cfg["min_edge"]
    howto = HOWTO.replace("{need}", signed_cents(need))
    plays = [b for b in board if b["signal"]]
    if plays:
        b = max(plays, key=lambda p: p["signal"]["ev"])
        kind, head, why, x = verdict(b, need)
        return (f'<section class="hero live"><p class="kicker">Paper play</p><h2>{head[12:13].upper() + head[13:]}</h2>'
                f'<p class="sub">{escape(b["matchup"])}, {when(b["start"])} HST. {why}</p>'
                f'{ten_dollar(x) if x else ""}{howto}</section>')
    if not board:
        return ('<section class="hero"><h2>No games on the board yet</h2><p class="sub">To stay on the '
                'free odds plan, the bot only checks sportsbooks for games ending within about 12 hours. '
                'Check back closer to game time.</p>' + howto + '</section>')
    fresh = [b for b in board if not b["signal"] and (not b["why"] or b["why"] == "no edge")]
    cands = [(best_side(b), b) for b in fresh if best_side(b)]
    if not cands:
        return ('<section class="hero"><h2>Waiting for fresh odds</h2><p class="sub">There are '
                f'{len(board)} games on the board, but their sportsbook odds are more than 30 minutes '
                'old. The bot refreshes them a few times a day to stay on the free plan, and only '
                'judges games on fresh prices. Check back after the next refresh.</p>' + howto + '</section>')
    x, b = max(cands, key=lambda t: t[0]["ev"])
    act, note = action(b, x)
    note = (role_note(x) + " " + note).strip()
    return (f'<section class="hero"><h2>No play right now</h2>'
            f'<p class="sub">Nothing on the board clears the {signed_cents(need)} bar. The closest is '
            f'{escape(b["matchup"])}: {act}. That\'s {signed_cents(x["ev"])} per contract on '
            f'average, so the bot passes. {note}</p>{howto}</section>')


def max_price(fair, need, fee_mult=1.0):
    """Highest Kalshi price (whole cents) at which the bet still clears the bot's edge bar."""
    best = None
    for c in range(1, 100):
        p = c / 100
        if fair - (p + 0.07 * fee_mult * p * (1 - p)) >= need:
            best = p
    return best


def plays_box(board, cfg, store):
    """The one thing the page must answer: what do I bet right now?"""
    plays = sorted([b for b in board if b["signal"]], key=lambda b: -b["signal"]["ev"])
    if not plays:
        lines = []
        for b in sorted(board, key=lambda b: b["start"]):
            need = cfg["series_min_edge"].get(b["series"], cfg["min_edge"])
            x = best_side(b)
            if b["why"] and b["why"] != "no edge":
                head = SKIP_TEXT.get(b["why"].split(":")[0], (b["why"], ""))[0]
                why = head
                if head == "Waiting for fresh odds":
                    why = "odds are old, waiting for a refresh"
                    if x is not None:
                        why += (f' (at the last refresh, the best was {escape(x["name"])} at {cents(x["ask"])}, '
                                f'{signed_cents(x["ev"])} per contract)')
            elif x is None:
                why = "no Kalshi price yet"
            elif x["ev"] <= 0:
                why = f'both sides cost more than they\'re worth (best: {escape(x["name"])} {signed_cents(x["ev"])})'
            else:
                why = (f'closest is {escape(x["name"])} at {cents(x["ask"])}, {signed_cents(x["ev"])} per '
                       f'contract, too small (needs {signed_cents(need)})')
            lines.append(f'<li><b>{escape(b["matchup"])}</b>: pass. {why[0].upper() + why[1:]}.</li>')
        checked = (f'<p>Checked {len(board)} game{"s" if len(board) != 1 else ""}. None is worth betting:</p>'
                   f'<ul class="passes">{"".join(lines)}</ul>') if board else ""
        return ('<section class="plays none"><h2>No plays right now</h2>' + checked +
                '<p class="fine">When the bot finds a bet, it shows up here as one line: what to buy on '
                'Kalshi and the most to pay.</p></section>')
    opens = {r["event_ticker"]: r for r in store.open_trades()}
    cards = []
    for b in plays:
        sg = b["signal"]
        need = cfg["series_min_edge"].get(b["series"], cfg["min_edge"])
        team = sg["team"]
        if sg["side"] == "no":
            opp = next((x["name"] for x in b["sides"] if x["name"] != team), "")
            what = (f'On Kalshi, buy <b>NO</b> on {escape(opp)}</p><p class="also">That\'s a bet on '
                    f'{escape(team)} to win.')
        else:
            what = f'On Kalshi, buy <b>YES</b> on {escape(team)}'
        mp = max_price(sg["prob"], need)
        pay = (f'It\'s {cents(sg["ask"])} now. Pay {cents(mp)} or less per contract.' if mp
               else f'It\'s {cents(sg["ask"])} now.')
        r = opens.get(b.get("event_ticker", ""))
        size = (f' Paper size: {r["contracts"]} contracts (${r["price"] * r["contracts"] + r["fee"]:,.2f}).'
                if r else "")
        cards.append(f'<article class="play"><p class="do">{what}</p>'
                     f'<p>{pay} Each contract pays $1 if {escape(team)} wins.{size}</p>'
                     f'<p class="when">{escape(b["matchup"])}, {when(b["start"])} HST. '
                     f'<a href="https://kalshi.com/markets/{b["series"].lower()}">Open on Kalshi</a></p></article>')
    n = len(plays)
    return (f'<section class="plays"><h2>{"1 play" if n == 1 else f"{n} plays"} right now</h2>'
            f'{"".join(cards)}<p class="fine">Paper trading only. These are the bot\'s picks, not advice.</p></section>')


def board_html(board, cfg):
    if not board:
        return ""
    groups = {}
    for b in sorted(board, key=lambda b: b["start"]):
        groups.setdefault(b["series"], []).append(b)
    out = ['<section><h2>The board</h2><p class="note">Games starting in the next 36 hours that '
           'matched a sportsbook game.</p>']
    for series, games in groups.items():
        need = cfg["series_min_edge"].get(series, cfg["min_edge"])
        link = (f' <a class="klink" href="https://kalshi.com/markets/{series.lower()}">'
                f'Open these on Kalshi</a>')
        out.append(f'<h3>{escape(SPORT.get(series, series))}{link}</h3><div class="games">')
        for b in games:
            kind, head, why, x = verdict(b, need)
            rows = []
            for s in b["sides"]:
                ev = s["ev"]
                via = (f'<span class="via">via No on {escape(other_side(b, s)["name"])}</span>'
                       if s.get("route") == "no on opponent" else "")
                tag = ('<span class="role fav">favorite</span>' if s["fair"] >= 0.5
                       else '<span class="role dog">underdog</span>')
                rows.append(
                    f'<tr><td>{escape(s["name"])} {tag}{via}</td>'
                    f'<td class="num">{cents(s["ask"]) if s["ask"] else "—"}</td>'
                    f'<td class="num">{pct(s["fair"])}</td>'
                    f'<td class="num {"up" if ev is not None and ev >= need else "flat"}">'
                    f'{signed_cents(ev) if ev is not None else "—"}</td></tr>')
            out.append(
                f'<article class="game {kind}"><header><span class="t">{when(b["start"])} HST</span>'
                f'<span class="m">{escape(b["matchup"])}</span></header>'
                f'<p class="verdict">{head}</p><p class="why">{why}</p>'
                f'{ten_dollar(x) if kind == "play" and x else ""}'
                f'<table class="sides"><thead><tr><th></th><th class="num">Kalshi price</th>'
                f'<th class="num">Sportsbooks\' chance</th><th class="num">Avg. profit per contract</th>'
                f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
                f'<p class="books">Based on {b["books"]} sportsbooks</p></article>')
        out.append("</div>")
    out.append("</section>")
    return "".join(out)


def trades_html(store, cfg):
    bankroll, exposure = store.bankroll(cfg)
    opens = store.open_trades()
    settled = store.settled_trades(25)
    out = ['<section><h2>Paper trades</h2>']
    allset = store.settled_trades()
    if allset:
        wins = sum(1 for r in allset if r["result"] == r["side"])
        cost = sum(r["price"] * r["contracts"] + r["fee"] for r in allset)
        pnl = sum(r["pnl"] or 0 for r in allset)
        exp = sum(r["prob"] for r in allset) / len(allset)
        clv = [r["clv"] for r in allset if r["clv"] is not None]
        out.append(f'<p class="stats"><span><b>{wins}–{len(allset) - wins}</b> record</span>'
                   f'<span><b>{wins / len(allset):.0%}</b> won vs {exp:.0%} expected</span>'
                   f'<span><b class="{"up" if pnl >= 0 else "down"}">{"+" if pnl >= 0 else "−"}${abs(pnl):,.2f}</b> '
                   f'({pnl / cost:+.1%} on ${cost:,.0f} risked)</span>'
                   + (f'<span><b>{sum(clv) / len(clv) * 100:+.1f}¢</b> average price move after entry</span>'
                      if clv else "") + '</p>')
        if len(allset) < 100:
            out.append(f'<p class="note">{len(allset)} settled so far. Under about 100, the record is '
                       'mostly luck either way.</p>')
    else:
        out.append('<p class="note">No paper trades have settled yet.</p>')
    if opens:
        out.append('<h3>Open</h3><table><thead><tr><th>Game</th><th>Pick</th><th class="num">Price</th>'
                   '<th class="num">Contracts</th><th>Starts</th></tr></thead><tbody>')
        for r in opens:
            out.append(f'<tr><td>{escape(r["matchup"])}</td><td>{escape(r["team"])}</td>'
                       f'<td class="num">{cents(r["price"])}</td><td class="num">{r["contracts"]}</td>'
                       f'<td>{when(r["start_ts"])}</td></tr>')
        out.append('</tbody></table>')
    if settled:
        out.append('<h3>Settled</h3><table><thead><tr><th>Game</th><th>Pick</th><th class="num">Price</th>'
                   '<th>Result</th><th class="num">P&amp;L</th></tr></thead><tbody>')
        for r in settled:
            won = r["result"] == r["side"]
            out.append(f'<tr><td>{escape(r["matchup"])}</td><td>{escape(r["team"])}</td>'
                       f'<td class="num">{cents(r["price"])}</td><td>{"Won" if won else "Lost"}</td>'
                       f'<td class="num {"up" if (r["pnl"] or 0) >= 0 else "down"}">'
                       f'{"+" if (r["pnl"] or 0) >= 0 else "−"}${abs(r["pnl"] or 0):,.2f}</td></tr>')
        out.append('</tbody></table>')
    out.append(f'<p class="note">Paper bankroll ${bankroll:,.2f} (started at ${cfg["bankroll"]:,.0f}); '
               f'${exposure:,.2f} in open plays.</p></section>')
    return "".join(out)


def lessons_html(store):
    pms = store.q("""SELECT p.ts, p.verdict, t.matchup, t.team FROM postmortems p
                     JOIN trades t ON t.id=p.trade_id ORDER BY p.ts DESC LIMIT 8""")
    learned = store.q("SELECT ts, change FROM learn_log ORDER BY ts DESC LIMIT 8")
    if not pms and not learned:
        return ""
    out = ['<section><h2>What the bot learned</h2>']
    if learned:
        out.append('<h3>Setting changes</h3><ul>' + "".join(
            f'<li><span class="t">{hst(parse_ts(r["ts"]))}</span> {escape(r["change"])}</li>' for r in learned)
            + '</ul>')
    if pms:
        out.append('<h3>Why recent losses lost</h3><ul>' + "".join(
            f'<li><b>{escape(r["team"])}</b> in {escape(r["matchup"])}: {escape(r["verdict"])}</li>'
            for r in pms) + '</ul>')
    out.append('</section>')
    return "".join(out)


def anomalies_html(an):
    if not an or not an.get("flagged"):
        return ""
    items = []
    for a in an["flagged"][:8]:
        title = a.get("event_title") or a.get("title") or "Kalshi market"
        option = a.get("option") or ""
        sub = a.get("event_sub") or ""
        lines = []
        if a.get("spread", 0) >= 0.10:
            if a["ask"] >= 0.99:
                lines.append(f'Buyers are offering {cents(a["bid"])}, but no one is selling right now.')
            elif a["bid"] <= 0.01:
                lines.append(f'Sellers want {cents(a["ask"])}, but no one is buying right now.')
            else:
                lines.append(f'Buyers are offering {cents(a["bid"])} but sellers want {cents(a["ask"])}, '
                             f'a {cents(a["spread"])} gap. A gap that wide means traders don\'t agree on '
                             f'the price yet.')
        if abs(a.get("move", 0)) >= 0.10:
            lines.append(f'The price {"jumped" if a["move"] > 0 else "dropped"} from '
                         f'{cents(a["prev"])} to {cents(a["last"])} in the last day.')
        lines.append(f'{a["vol24"]:,.0f} contracts traded in the last 24 hours'
                     + (f', and it settles {when(a["resolves"].isoformat())} HST.' if a.get("resolves") else "."))
        link = (f' <a href="https://kalshi.com/markets/{escape(a["series_ticker"].lower())}">'
                f'Open on Kalshi</a>' if a.get("series_ticker") else "")
        opt = (f'<p class="opt">Option: <b>{escape(option)}</b></p>'
               if option and option.lower() not in title.lower() else "")
        items.append(f'<li><p class="q">{escape(title)}</p>'
                     + (f'<p class="opt">{escape(sub)}</p>' if sub and sub.lower() not in title.lower() else "")
                     + f'{opt}<p>{" ".join(escape(x) for x in lines)}{link}</p></li>')
    return (f'<section><h2>Unusual Kalshi markets</h2><p class="note">Markets with real trading in the '
            f'last day that settle within 3 days, where the price moved 10¢ or more in a day, or buyers '
            f'and sellers are 10¢ or more apart. They aren\'t plays: the bot has no sportsbook price to '
            f'check them against. They\'re just worth a look.</p><ul class="anom">{"".join(items)}</ul></section>')


# ================================================================ PICK MODE pages

TYPE_TEXT = {"favorite": "Favorite", "upset": "Underdog pick (upset coin)",
             "injury": "Underdog pick (injury news on the favorite)", "edge": "Edge play"}


def opponent_of(matchup, team):
    for sep in (" @ ", " vs "):
        if sep in matchup:
            a, b = matchup.split(sep, 1)
            return b if a == team else a
    return ""


def top_pick_key(r):
    """Top Pick order (the card order): highest win chance first; ties: sportsbook-backed first."""
    return (-r["prob"], 0 if r["n_books"] else 1, r["start_ts"] or "")


def bet_value(r):
    """Best Bet value: average return per dollar = win chance / cost per contract (incl. fee) - 1.
    Positive means the price is cheap for that chance; negative means you're paying up."""
    cost = r["price"] + (r["fee"] / r["contracts"] if r["contracts"] else 0)
    return r["prob"] / cost - 1 if cost > 0 else -1


def open_picks_box(store, cfg):
    rows = sorted(store.open_trades(), key=top_pick_key)
    # Value needs an outside price: Kalshi-only picks would just be compared with themselves
    priced = [r for r in rows if r["n_books"]]
    by_value = sorted(priced, key=lambda r: (-bet_value(r), top_pick_key(r)))
    best_bet = {r["id"]: i for i, r in enumerate(by_value, 1)}
    now = now_utc()
    if not rows:
        return ('<section class="plays none"><h2>No open picks right now</h2><p>The bot picks every '
                'matched game starting in the next 36 hours. New picks show up here after the next '
                'hourly run.</p></section>')
    cards = []
    n = len(rows)
    for rank, r in enumerate(rows, 1):
        opp = opponent_of(r["matchup"], r["team"])
        started = parse_ts(r["start_ts"]) and parse_ts(r["start_ts"]) <= now
        kind = r["pick_type"] or "edge"
        cost = r["price"] * r["contracts"] + r["fee"]
        cards.append(
            f'<article class="play {"dog" if kind in ("upset", "injury") else "fav"}">'
            f'<p class="sport">{escape(SPORT.get(r["series"], r["series"]))}</p>'
            f'<p class="do">#{rank} {escape(r["team"])}{" to beat " + escape(opp) if opp else ""}</p>'
            f'<p><span class="tag {"play" if kind == "favorite" else "dogtag"}">{TYPE_TEXT.get(kind, kind)}</span> '
            f'{"Kalshi prices give" if not r["n_books"] else "Sportsbooks give"} {escape(r["team"])} '
            f'{r["prob"] * 100:.0f}%.</p>'
            f'<p class="when"><span class="start" data-start="{escape(r["start_ts"] or "")}" '
            f'data-est="{0 if r["n_books"] else 1}" data-tennis="{1 if r["series"] in TENNIS else 0}">'
            f'{start_phrase(r["start_ts"], now, not r["n_books"], r["series"] in TENNIS)}</span> '
            f'Paper: {r["contracts"]} contracts at '
            f'{cents(r["price"])} (${cost:,.2f}), pays ${r["contracts"]:,.2f} if they win.</p>'
            + (f'<p class="rank">Best Bet #{best_bet[r["id"]]} of {len(priced)}</p></article>' if r["id"] in best_bet
               else '<p class="rank muted">Best Bet: not ranked (no sportsbook price to compare)</p></article>'))
    return (f'<section class="plays"><h2>Top Picks</h2><p class="fine top">{n} open. The number in front '
            f'ranks by win chance. Best Bet ranks the sportsbook-priced picks by value: how the win chance '
            f'compares with the price.</p>'
            f'{"".join(cards)}'
            f'<p class="fine">For fun only: paper bets, nothing real is bought.</p></section>')


def picks_record_html(store, cfg):
    allset = [r for r in store.settled_trades()]
    out = ['<section><h2>Record</h2>']
    if not allset:
        out.append('<p class="note">No picks have finished yet. Results show up here about an hour '
                   'after each game ends.</p></section>')
        return "".join(out)
    def rec(rows):
        w = sum(1 for r in rows if r["result"] == r["side"])
        return w, len(rows) - w
    w, l = rec(allset)
    pnl = sum(r["pnl"] or 0 for r in allset)
    fav = [r for r in allset if (r["pick_type"] or "") == "favorite"]
    dog = [r for r in allset if (r["pick_type"] or "") in ("upset", "injury")]
    exp = sum(r["prob"] for r in allset)
    out.append(f'<p class="stats"><span><b>{w}–{l}</b> overall</span>'
               + (f'<span><b>{rec(fav)[0]}–{rec(fav)[1]}</b> favorites</span>' if fav else "")
               + (f'<span><b>{rec(dog)[0]}–{rec(dog)[1]}</b> underdog picks</span>' if dog else "")
               + f'<span><b class="{"up" if pnl >= 0 else "down"}">{"+" if pnl >= 0 else "−"}${abs(pnl):,.2f}</b> paper P&amp;L</span></p>'
               f'<p class="note">Sportsbook odds said to expect about {exp:.1f} wins from these '
               f'{len(allset)} picks.</p>')
    out.append('<table class="board"><thead><tr><th>Sport</th><th>Game</th><th>Pick</th><th>Type</th>'
               '<th>Result</th><th class="num">P&amp;L</th></tr></thead><tbody>')
    for r in allset[:30]:
        won = r["result"] == r["side"]
        out.append(f'<tr><td>{escape(SPORT.get(r["series"], r["series"]))}</td>'
                   f'<td>{escape(r["matchup"])}</td><td>{escape(r["team"])} '
                   f'({r["prob"] * 100:.0f}%)</td><td>{TYPE_TEXT.get(r["pick_type"] or "edge", "")}</td>'
                   f'<td>{"Won" if won else "Lost"}</td><td class="num {"up" if (r["pnl"] or 0) >= 0 else "down"}">'
                   f'{"+" if (r["pnl"] or 0) >= 0 else "−"}${abs(r["pnl"] or 0):,.2f}</td></tr>')
    out.append('</tbody></table></section>')
    return "".join(out)


def picks_board_html(board):
    if not board:
        return ""
    out = ['<section><h2>Every game checked this run</h2><table class="board"><thead><tr><th>Time (HST)</th>'
           '<th>Game</th><th>Pick</th><th class="num">Chance</th><th>Status</th></tr></thead><tbody>']
    for b in sorted(board, key=lambda b: b["start"]):
        pk = b["pick"]
        if pk:
            status = {"open": "Picked", "new": "Picked (not saved: preview run)",
                      "blocked": f'Not opened: {escape(b["why"] or "")}'}.get(pk["status"], pk["status"])
            pick = f'{escape(pk["team"])}<span class="via">{TYPE_TEXT.get(pk["type"], "")}</span>'
            chance = f'{pk["prob"] * 100:.0f}%'
        else:
            status, pick, chance = f'Skipped: {escape(b["why"] or "")}', "—", ""
        age = b.get("odds_age_min")
        books = (f'{b["books"]} sportsbooks' + (f', odds {age / 60:.0f}h old' if age and age >= 90 else "")
                 if b.get("source", "sportsbooks") == "sportsbooks" else "no sportsbook line: Kalshi prices, start time estimated")
        out.append(f'<tr><td>{when(b["start"])}</td><td><span class="sport">{escape(SPORT.get(b["series"], b["series"]))}'
                   f'</span>{escape(b["matchup"])}<span class="via">{books}</span></td>'
                   f'<td>{pick}</td><td class="num">{chance}</td><td>{status}</td></tr>')
    out.append('</tbody></table></section>')
    return "".join(out)


def skipped_html(skipped):
    if not skipped:
        return ""
    rows = sorted(skipped, key=lambda x: x["ends"])
    items = "".join(f'<li><b>{escape(" vs ".join(n for n in x["names"] if n))}</b> '
                    f'<span class="via">{escape(SPORT.get(x["series"], x["series"]))}, ends about '
                    f'{when(x["ends"])} HST</span>{escape(x["reason"][0].upper() + x["reason"][1:])}.</li>'
                    for x in rows[:40])
    more = f'<p class="note">And {len(rows) - 40} more.</p>' if len(rows) > 40 else ""
    return (f'<section><h2>Games skipped</h2><p class="note">Kalshi games in the next 36 hours the bot '
            f'couldn\'t pick, and why.</p><ul class="skips">{items}</ul>{more}</section>')


def picks_howto(cfg):
    dog = round(cfg["pick_upset_rate"] * 100)
    return (f'<details class="howto"><summary>How the bot picks</summary><ul>'
            f'<li>For every Kalshi game starting in the next 36 hours, it backs the side the sportsbooks give '
            f'the better chance to win. If no sportsbook lists the game, it uses Kalshi\'s own prices '
            f'instead, but only when they\'re tight enough to trust. Soccer picks are always a team to win, '
            f'never a tie.</li>'
            f'<li>About {dog} games in 100, a coin flip sends it to the underdog instead. ' +
            ('If recent news mentions injuries for the favorite, it also takes the underdog. '
             if cfg["pick_injury_override"] else '') + '</li>'
            f'<li>A pick makes the slate only with at least a {cfg["pick_min_win_chance"] * 100:.0f}% win '
            f'chance, or as a clear value underdog: priced by sportsbooks and returning at least '
            f'{cfg["pick_value_underdog_min_return"] * 100:.0f}% more than it costs, on average.</li>'
            f'<li>The slate holds up to {cfg["max_open_trades"]} picks, highest win chance first, with about '
            f'a quarter of the spots kept for value underdogs. At most {cfg["max_trades_per_day"]} new picks '
            f'a day. Each pick is a ${cfg["pick_stake_dollars"]:,.0f} paper bet at the Kalshi price; a '
            f'contract pays $1 if your side wins.</li>'
            f'<li><b>Top Pick #</b> ranks open picks by win chance. <b>Best Bet #</b> ranks them by value: '
            f'win chance divided by what a contract costs, including Kalshi\'s fee. A 30% underdog at 25¢ '
            f'can be a better bet than an 80% favorite at 85¢.</li>'
            f'<li>Favorites still lose often, and paying the favorite\'s price usually costs a little over '
            f'time. This is for watching games, not a money-making system.</li></ul></details>')


def render_picks(cfg, store, board, anomalies, skipped):
    now = now_utc()
    bankroll, _ = store.bankroll(cfg)
    allset = store.settled_trades()
    wins = sum(1 for r in allset if r["result"] == r["side"])
    strip = (f'<dl class="strip"><div><dt>Record</dt><dd>{wins}–{len(allset) - wins}</dd></div>'
             f'<div><dt>Open picks</dt><dd>{store.open_count()}</dd></div>'
             f'<div><dt>Paper bankroll</dt><dd>${bankroll:,.0f}</dd></div>'
             f'</dl>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TheEsness Kalshi picks</title>
<meta name="description" content="Paper picks on Kalshi college and pro sports games: the favorite, with the odd underdog.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;600;700&family=Source+Sans+3:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main>
<header><h1>TheEsness Kalshi picks</h1>
<p class="clock">Now: <b id="now">{escape(hst(now))}</b></p>
<p class="updated" data-updated="{now.isoformat()}">Updated {escape(hst(now))}<span id="ago"></span>. Refreshes about every hour. Paper picks for fun.</p>
<p id="stale" class="stale" hidden></p>{strip}</header>
{open_picks_box(store, cfg)}
{picks_howto(cfg)}
<footer><p>Win chances are the average of several sportsbooks' moneylines with their built-in margin
removed. Picks are paper bets only. No real orders are ever placed, and nothing here is betting advice.</p></footer>
</main>
<script>
/* Every time on this page is recomputed here from UTC timestamps and shown in Hawaii time
   (Pacific/Honolulu), whatever timezone this device is set to. Runs every 30 seconds. */
(function () {{
  var TZ = "Pacific/Honolulu";
  var dayTime = new Intl.DateTimeFormat("en-US", {{timeZone: TZ, weekday: "short", hour: "numeric", minute: "2-digit"}});
  var full = new Intl.DateTimeFormat("en-US", {{timeZone: TZ, weekday: "short", month: "2-digit", day: "2-digit",
                                               hour: "2-digit", minute: "2-digit", hour12: true}});
  function clockText(d) {{
    var p = {{}};
    full.formatToParts(d).forEach(function (x) {{ p[x.type] = x.value; }});
    return p.weekday + " " + p.month + "/" + p.day + " " + p.hour + ":" + p.minute + " " + p.dayPeriod + " HST";
  }}
  function hst(d) {{ return dayTime.format(d).replace(",", "") + " HST"; }}
  function span(mins) {{
    var m = Math.round(Math.abs(mins));
    return m >= 60 ? Math.floor(m / 60) + "h " + (m % 60) + "m" : m + "m";
  }}
  function phrase(d, est, tennis, now) {{
    var mins = (d - now) / 60000, t = hst(d);
    if (mins > 0) {{
      if (tennis) return "Scheduled " + t + " (in " + span(mins) + "; tennis order of play can slip).";
      if (est) return "Starts about " + t + " (in " + span(mins) + "; estimated from Kalshi).";
      return "Starts " + t + " (in " + span(mins) + ").";
    }}
    var lead = tennis ? "Scheduled for" : (est ? "Started about" : "Started");
    return lead + " " + t + " (" + span(mins) + " ago), waiting for the result.";
  }}
  function tick() {{
    var now = new Date();
    document.getElementById("now").textContent = clockText(now);
    var up = document.querySelector("[data-updated]"), upd = new Date(up.getAttribute("data-updated"));
    var age = (now - upd) / 60000;
    document.getElementById("ago").textContent = " (" + span(age) + " ago)";
    var st = document.getElementById("stale");
    if (age > 90) {{
      st.hidden = false;
      st.textContent = "This page hasn't updated in " + span(age) + ". It normally refreshes every hour, "
        + "so picks and results may be out of date. Reload the page to check.";
    }} else {{ st.hidden = true; }}
    document.querySelectorAll("[data-start]").forEach(function (el) {{
      var d = new Date(el.getAttribute("data-start"));
      if (!isNaN(d)) el.textContent = phrase(d, el.dataset.est === "1", el.dataset.tennis === "1", now);
    }});
  }}
  /* Pull in a newer build when one is published (skips the browser and CDN caches). */
  function checkForUpdate() {{
    var mine = document.querySelector("[data-updated]").getAttribute("data-updated");
    fetch("board.json?t=" + Date.now(), {{cache: "no-store"}}).then(function (r) {{ return r.json(); }})
      .then(function (j) {{ if (j.updated && j.updated !== mine) location.replace(location.pathname + "?r=" + Date.now()); }})
      .catch(function () {{}});
  }}
  tick();
  setInterval(tick, 30000);
  setInterval(checkForUpdate, 5 * 60000);
}})();
</script>
</body></html>"""


CSS = """
.play.dog .do{color:var(--kal)}
.play .rank{margin-top:6px;font:700 1.05rem/1.2 "Saira Condensed",sans-serif;color:var(--ink)}
.plays .fine.top{margin:-4px 0 6px}
.clock{margin:6px 0 0;color:var(--ink)}
.stale{margin:8px 0 0;padding:8px 12px;border-radius:4px;background:var(--cran);color:var(--card);font-weight:600;max-width:70ch}
.play .rank.muted{font:400 .9rem/1.3 "Source Sans 3",sans-serif;color:var(--soft)}
.sport{display:block;margin:0 0 2px;font:600 .8rem/1.2 "Source Sans 3",sans-serif;color:var(--soft);letter-spacing:.02em}
.play .sport{font-size:.95rem;color:var(--ink)}
.tag.dogtag{background:var(--kal);color:var(--card);font-weight:600}
.board td{font-size:.95rem}.board .via{display:block}
.skips{list-style:none;padding:0;margin:0}.skips li{padding:6px 0;border-bottom:1px solid var(--rule);max-width:none}
.skips .via{display:block}
.tag{white-space:normal}
@media (max-width:560px){.board thead{display:none}.board tr{display:block;padding:8px 0;border-bottom:1px solid var(--rule)}
.board td{display:block;border:0;padding:1px 0;text-align:left}.board td:first-child{color:var(--soft);font-size:.85rem}
.board td.num{text-align:left}.board td.num:not(:empty)::before{content:"Sportsbooks give it "}.play .do{font-size:1.6rem}}

.plays{margin-top:28px;padding:22px;background:var(--card);border:2px solid var(--rule);border-radius:6px}
.plays h2{font-size:2.4rem;margin:0 0 10px}
.plays.none h2{color:var(--soft)}
.plays p{margin:4px 0;max-width:62ch;font-size:1.1rem}
.play{padding:14px 0;border-top:1px solid var(--rule)}.play:first-of-type{border-top:0;padding-top:4px}
.play .do{font:700 2rem/1.15 "Saira Condensed",sans-serif;color:var(--pine);margin:0 0 6px}
.play .do b{font-weight:700}
.play .also{color:var(--soft);margin-top:-4px}
.play .when{color:var(--soft);font-size:1rem}
.passes{margin:6px 0 4px;padding-left:20px}.passes li{margin:4px 0;max-width:70ch}
.plays .fine{color:var(--soft);font-size:.9rem;margin-top:10px}
details.more{margin-top:28px}
details.more>summary{cursor:pointer;font-weight:600;font-size:1.05rem;color:var(--kal)}

.howto{margin:16px 0 0;border-top:1px solid var(--rule);padding-top:12px}
.howto summary{cursor:pointer;font-weight:600}
.howto ul{margin:8px 0 0}.howto li{max-width:70ch}
.verdict{margin:2px 0 2px;font:700 1.35rem/1.15 "Saira Condensed",sans-serif}
.game.play{border-color:var(--pine);border-width:2px}.game.play .verdict{color:var(--pine)}
.game.skip .verdict{color:var(--soft)}
.why{margin:0 0 8px;max-width:70ch}
.eg{margin:0 0 10px;padding:8px 12px;background:var(--pine-tint);border-radius:4px;max-width:70ch}
.sides{margin-top:4px}.sides th{font-size:.8rem;font-weight:600}.sides td{font-size:1rem}
.sides td.num{font-family:"Saira Condensed",sans-serif;font-size:1.15rem}
.via{display:block;color:var(--soft);font-size:.8rem}
.game .books{margin:6px 0 0;color:var(--soft);font-size:.8rem}
.role{font-size:.75rem;padding:0 6px;border-radius:8px;margin-left:4px;vertical-align:1px;background:var(--track);color:var(--soft)}.role.fav{color:var(--ink)}
.klink{font:400 .9rem "Source Sans 3",sans-serif;margin-left:10px}
:root{--paper:#eef2f6;--card:#f8fafc;--ink:#18222f;--soft:#5b6878;--rule:#cfd7e1;--pine:#0d7a63;
--pine-tint:#cbe9df;--cran:#a33a57;--kal:#2f5bea;--track:#dde4ec}
@media (prefers-color-scheme:dark){:root{--paper:#10161e;--card:#161e28;--ink:#e4eaf1;--soft:#9aa7b6;
--rule:#2a3542;--pine:#3fbf9a;--pine-tint:#1d4a3e;--cran:#e07a95;--kal:#7d9bff;--track:#24303d}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:400 16px/1.55 "Source Sans 3",system-ui,sans-serif}
main{max-width:920px;margin:0 auto;padding:28px 16px 64px}
h1,h2,h3,.num,.t,.stats b{font-family:"Saira Condensed","Arial Narrow",sans-serif;font-variant-numeric:tabular-nums}
h1{font-size:2.6rem;line-height:1;margin:0 0 6px;font-weight:700;letter-spacing:-.01em}
h2{font-size:1.75rem;line-height:1.1;margin:0 0 6px;font-weight:700}
h3{font-size:1.2rem;margin:26px 0 8px;font-weight:600;color:var(--soft)}
section{margin-top:44px}
.updated{color:var(--soft);margin:0}
.strip{display:flex;flex-wrap:wrap;gap:8px 28px;margin:16px 0 0;padding:0}
.strip div{display:flex;flex-direction:column}
.strip dt{color:var(--soft);font-size:.85rem}
.strip dd{margin:0;font:600 1.35rem/1.1 "Saira Condensed",sans-serif;font-variant-numeric:tabular-nums}
.hero{margin-top:32px;padding:24px 22px 18px;background:var(--card);border-left:5px solid var(--rule);border-radius:4px}
.hero.live{border-left-color:var(--pine)}
.hero h2{font-size:2.3rem}
.kicker{margin:0 0 4px;color:var(--pine);font-weight:600}
.sub{margin:0 0 18px;max-width:62ch}
.gap{position:relative;height:10px;background:var(--track);border-radius:5px;margin:6px 0}
.hero .gap{height:16px;border-radius:8px}
.gap .span{position:absolute;top:0;bottom:0;background:var(--soft);opacity:.35;border-radius:inherit}
.gap.pos .span{background:var(--pine);opacity:1}
.gap .mk{position:absolute;top:-3px;bottom:-3px;width:3px;margin-left:-1.5px;border-radius:2px}
.hero .gap .mk{top:-5px;bottom:-5px;width:4px;margin-left:-2px}
.mk.k{background:var(--kal)}.mk.s{background:var(--ink)}
.legend{color:var(--soft);font-size:.85rem;margin:10px 0 0;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}
.key{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.key.k{background:var(--kal);width:4px}.key.s{background:var(--ink);width:4px}.key.span-pos{background:var(--pine)}
.note{color:var(--soft);margin:0 0 10px;max-width:68ch}
.games{display:grid;gap:10px}
.game{background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:12px 14px}
.game header{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;margin-bottom:6px}
.game .t{color:var(--soft);font-size:1.05rem}
.game .m{font-weight:600}
.books{margin-left:auto;color:var(--soft);font-size:.85rem}
.tag{font-size:.8rem;padding:1px 8px;border-radius:10px;background:var(--track);color:var(--soft)}
.tag.play{background:var(--pine);color:var(--card);font-weight:600}
.cols,.side{display:grid;grid-template-columns:minmax(0,1fr) 56px 64px 64px;gap:2px 10px;align-items:center}
.cols{color:var(--soft);font-size:.8rem}.cols span:not(:first-child){text-align:right}
.side{padding:4px 0}.side .gap{grid-column:1/-1}
.side .num{text-align:right;font-size:1.1rem}
.who{overflow-wrap:anywhere}
.up{color:var(--pine);font-weight:600}.down{color:var(--cran);font-weight:600}.flat{color:var(--soft)}
.stats{display:flex;flex-wrap:wrap;gap:6px 26px;margin:0 0 8px}
.stats b{font-size:1.5rem;font-weight:700;margin-right:4px}
table{width:100%;border-collapse:collapse;font-size:.95rem}
th{text-align:left;font-weight:600;color:var(--soft);border-bottom:2px solid var(--rule);padding:6px 8px 6px 0}
td{border-bottom:1px solid var(--rule);padding:7px 8px 7px 0;vertical-align:top}
td.num,th.num{text-align:right}
ul{padding-left:20px;margin:0}li{margin:4px 0}
.anom{list-style:none;padding:0;display:grid;gap:10px}.anom li{margin:0;background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:10px 14px}.anom p{margin:2px 0;max-width:70ch}.anom .q{font-weight:600}.anom .opt{color:var(--soft)}
footer{margin-top:56px;padding-top:16px;border-top:1px solid var(--rule);color:var(--soft);font-size:.9rem}
footer p{max-width:70ch}
a{color:var(--kal)}a:focus-visible{outline:3px solid var(--kal);outline-offset:2px}
@media (max-width:560px){h1{font-size:2.1rem}.hero h2{font-size:1.85rem}
.cols,.side{grid-template-columns:minmax(0,1fr) 46px 54px 56px}.books{margin-left:0;width:100%}
table{font-size:.85rem}}
"""


def render(cfg, store, board, anomalies, model_active, skipped=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    now = now_utc()
    if cfg.get("mode") == "picks":
        with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_picks(cfg, store, board, anomalies, skipped))
        with open(os.path.join(OUT_DIR, ".nojekyll"), "w") as f:
            f.write("")
        with open(os.path.join(OUT_DIR, "board.json"), "w") as f:
            json.dump({"updated": now.isoformat(), "mode": "picks", "board": board,
                       "skipped": skipped or []}, f, indent=1)
        return
    bankroll, _ = store.bankroll(cfg)
    allset = store.settled_trades()
    wins = sum(1 for r in allset if r["result"] == r["side"])
    credits = store.get_meta("odds_remaining")
    strip = (f'<dl class="strip"><div><dt>Paper bankroll</dt><dd>${bankroll:,.0f}</dd></div>'
             f'<div><dt>Record</dt><dd>{wins}–{len(allset) - wins}</dd></div>'
             f'<div><dt>Open plays</dt><dd>{store.open_count()}</dd></div>'
             f'<div><dt>Games checked</dt><dd>{len(board)}</dd></div>'
             f'<div><dt>Free odds credits left</dt><dd>{credits if credits is not None else "—"}</dd></div>'
             f'<div><dt>Fair price from</dt><dd>{"Model" if model_active else "Sportsbooks"}</dd></div></dl>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi price board</title>
<meta name="description" content="Kalshi sports prices compared with sportsbook consensus, with paper-trade results.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;600;700&family=Source+Sans+3:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main>
<header><h1>Kalshi price board</h1>
<p class="updated">Updated {escape(hst(now))}. Refreshes about every hour. Paper trading only.</p>{strip}</header>
{plays_box(board, cfg, store)}
<details class="more"><summary>Details: every game the bot checked, paper results, and how it works</summary>
{HOWTO.replace("{need}", signed_cents(cfg["min_edge"]))}
{board_html(board, cfg)}
{trades_html(store, cfg)}
{lessons_html(store)}
{anomalies_html(anomalies)}
</details>
<footer><p>Each game's fair price is the average of several sportsbooks' moneylines with their
built-in margin removed. A paper play needs Kalshi's price to be at least {cfg["min_edge"] * 100:.0f}¢ below that after
Kalshi's taker fee. No real orders are ever placed, and nothing here is betting advice.</p></footer>
</main></body></html>"""
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    with open(os.path.join(OUT_DIR, ".nojekyll"), "w") as f:
        f.write("")
    with open(os.path.join(OUT_DIR, "board.json"), "w") as f:
        json.dump({"updated": now.isoformat(), "board": board}, f, indent=1)
