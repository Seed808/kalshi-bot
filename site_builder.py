"""Builds the public price-board website (docs/index.html) after every cycle.

GitHub Pages serves the docs/ folder. Static HTML, no JavaScript, no external data calls:
everything shown is computed here from the bot's own database and the cycle's board.
"""
import json
import os
from datetime import timedelta
from html import escape

from common import now_utc, parse_ts, hst, HST

SPORT = {"KXNFLGAME": "NFL", "KXNBAGAME": "NBA", "KXMLBGAME": "MLB", "KXNHLGAME": "NHL",
         "KXNCAAFGAME": "College football", "KXATPMATCH": "Men's tennis (ATP)",
         "KXWTAMATCH": "Women's tennis (WTA)"}
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


def gap_bar(price, fair, edge, label, need=0.0):
    """0–100% track: Kalshi price marker, sportsbook fair marker, span between them."""
    lo, hi = sorted((price, fair))
    cls = "pos" if edge is not None and edge > 0 and edge >= need else "neg"
    aria = (f"{escape(label)}: Kalshi {cents(price)}, sportsbooks {pct(fair)}, "
            f"edge after fees {signed_cents(edge) if edge is not None else 'n/a'}")
    return (f'<div class="gap {cls}" role="img" aria-label="{aria}">'
            f'<span class="span" style="left:{lo * 100:.1f}%;width:{max(0.6, (hi - lo) * 100):.1f}%"></span>'
            f'<span class="mk k" style="left:{price * 100:.1f}%" title="Kalshi {cents(price)}"></span>'
            f'<span class="mk s" style="left:{fair * 100:.1f}%" title="Sportsbooks {pct(fair)}"></span>'
            f'</div>')


def hero(board, cfg):
    sigs = [b for b in board if b["signal"]]
    if sigs:
        b = max(sigs, key=lambda x: x["signal"]["ev"])
        s = b["signal"]
        side = next((x for x in b["sides"] if x["name"] == s["team"]), b["sides"][0])
        return (f'<section class="hero live"><p class="kicker">Paper play</p>'
                f'<h2>Back {escape(s["team"])} at {cents(s["ask"])}</h2>'
                f'<p class="sub">{escape(b["matchup"])}, {when(b["start"])} HST. Sportsbooks make it '
                f'{pct(s["prob"])}, which leaves <strong>{signed_cents(s["ev"])}</strong> per contract '
                f'after Kalshi fees.</p>{gap_bar(s["ask"], side["fair"], s["ev"], s["team"])}'
                f'{legend()}</section>')
    priced = [(x["ev"], b, x) for b in board for x in b["sides"] if x["ev"] is not None]
    if not priced:
        return ('<section class="hero"><h2>No games on the board yet</h2><p class="sub">The bot only '
                'buys sportsbook odds for games starting within about 12 hours, to stay inside the free '
                'plan. Check back closer to game time.</p></section>')
    ev, b, x = max(priced, key=lambda t: t[0])
    return (f'<section class="hero"><h2>No edge right now</h2>'
            f'<p class="sub">Closest: {escape(x["name"])} in {escape(b["matchup"])}. Kalshi asks '
            f'{cents(x["ask"])}, sportsbooks say {pct(x["fair"])}: {signed_cents(ev)} after fees. '
            f'A paper play needs at least {signed_cents(cfg["min_edge"])}.</p>'
            f'{gap_bar(x["ask"], x["fair"], ev, x["name"], cfg["min_edge"])}'
            f'{legend()}</section>')


def legend():
    return ('<p class="legend"><span class="key k"></span>Kalshi price '
            '<span class="key s"></span>Sportsbook fair price '
            '<span class="key span-pos"></span>Edge after fees</p>')


def board_html(board, cfg):
    if not board:
        return ""
    groups = {}
    for b in sorted(board, key=lambda b: b["start"]):
        groups.setdefault(b["series"], []).append(b)
    out = ['<section><h2>The board</h2><p class="note">Games starting in the next 36 hours that '
           'matched a sportsbook game. Kalshi prices are what one contract costs; it pays $1 if that '
           'side wins.</p>']
    for series, games in groups.items():
        need = cfg["series_min_edge"].get(series, cfg["min_edge"])
        out.append(f'<h3>{escape(SPORT.get(series, series))}</h3><div class="games">')
        for b in games:
            flag = ""
            if b["signal"]:
                flag = '<span class="tag play">Paper play</span>'
            elif b["why"] and b["why"] != "no edge":
                flag = f'<span class="tag">{escape(b["why"])}</span>'
            rows = []
            for x in b["sides"]:
                ev = x["ev"]
                evtxt = signed_cents(ev) if ev is not None else "—"
                rows.append(
                    f'<div class="side"><span class="who">{escape(x["name"])}</span>'
                    f'<span class="num">{cents(x["ask"]) if x["ask"] else "—"}</span>'
                    f'<span class="num">{pct(x["fair"])}</span>'
                    f'<span class="num {"up" if ev is not None and ev >= need else "flat"}">{evtxt}</span>'
                    f'{gap_bar(x["ask"], x["fair"], ev, x["name"], need) if x["ask"] else ""}</div>')
            out.append(f'<article class="game"><header><span class="t">{when(b["start"])}</span>'
                       f'<span class="m">{escape(b["matchup"])}</span>{flag}'
                       f'<span class="books">{b["books"]} sportsbooks</span></header>'
                       f'<div class="cols"><span></span><span>Kalshi</span><span>Fair</span>'
                       f'<span>Edge</span></div>{"".join(rows)}</article>')
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


CSS = """
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


def render(cfg, store, board, anomalies, model_active):
    os.makedirs(OUT_DIR, exist_ok=True)
    now = now_utc()
    bankroll, _ = store.bankroll(cfg)
    allset = store.settled_trades()
    wins = sum(1 for r in allset if r["result"] == r["side"])
    credits = store.get_meta("odds_remaining")
    strip = (f'<dl class="strip"><div><dt>Paper bankroll</dt><dd>${bankroll:,.0f}</dd></div>'
             f'<div><dt>Record</dt><dd>{wins}–{len(allset) - wins}</dd></div>'
             f'<div><dt>Open plays</dt><dd>{store.open_count()}</dd></div>'
             f'<div><dt>Games on board</dt><dd>{len(board)}</dd></div>'
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
{hero(board, cfg)}
{board_html(board, cfg)}
{trades_html(store, cfg)}
{lessons_html(store)}
{anomalies_html(anomalies)}
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
