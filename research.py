"""STEP 2 — Research agents (run in parallel).

Sources: ESPN RSS feeds (free), Reddit (needs approved OAuth app), X recent search
(pay-per-use, hard monthly read cap). Each team gets: mention count, average VADER
sentiment of posts mentioning it, and injury-keyword hits. No LLM.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

from common import now_utc

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _VADER = SentimentIntensityAnalyzer()
except Exception:  # pragma: no cover
    _VADER = None

UA = os.environ.get("REDDIT_USER_AGENT", "python:kalshibot:0.2 (by /u/your_username)")

INJURY_RE = re.compile(
    r"\b(ruled out|out for|will miss|won'?t play|injur\w*|questionable|doubtful|sidelined|"
    r"day-to-day|concussion|suspend\w*|placed on (?:the )?(?:il|ir|injured))\b", re.I)


def team_aliases(name):
    n = name.lower().strip()
    al = {n}
    parts = n.split()
    if len(parts) > 1 and len(parts[-1]) > 3:
        al.add(parts[-1])
    return al


# ---------------------------------------------------------------- sources

def fetch_rss(urls, lookback_h):
    if not feedparser:
        return []
    cutoff = now_utc() - timedelta(hours=lookback_h)
    items = []
    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "kalshibot/0.2"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            print(f"RSS error {url}: {e}")
            continue
        for e in feed.entries:
            pp = getattr(e, "published_parsed", None)
            if pp and datetime(*pp[:6], tzinfo=timezone.utc) < cutoff:
                continue
            items.append({"src": "rss", "text": f'{e.get("title", "")} {e.get("summary", "")}'})
    return items


_reddit = {"tok": None, "exp": 0.0}


def _reddit_token():
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        return None
    if _reddit["tok"] and now_utc().timestamp() < _reddit["exp"] - 60:
        return _reddit["tok"]
    r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                      data={"grant_type": "client_credentials"}, headers={"User-Agent": UA},
                      timeout=20)
    r.raise_for_status()
    d = r.json()
    _reddit["tok"], _reddit["exp"] = d["access_token"], now_utc().timestamp() + d.get("expires_in", 3600)
    return _reddit["tok"]


def fetch_reddit(subs, lookback_h):
    try:
        tok = _reddit_token()
    except Exception as e:
        print(f"Reddit auth error: {e}")
        return []
    if not tok:
        return []
    cutoff = (now_utc() - timedelta(hours=lookback_h)).timestamp()
    items = []
    for sub in subs:
        try:
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/new", params={"limit": 100},
                             headers={"Authorization": f"bearer {tok}", "User-Agent": UA}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            print(f"Reddit error r/{sub}: {e}")
            continue
        for c in r.json().get("data", {}).get("children", []):
            d = c.get("data", {})
            if d.get("created_utc", 0) < cutoff:
                continue
            items.append({"src": "reddit",
                          "text": f'{d.get("title", "")} {(d.get("selftext") or "")[:500]}'})
    return items


def x_batch_size(cfg):
    return max(10, min(100, int(cfg["x_max_results"])))


def fetch_x(team, cfg):
    """Runs in a worker thread; budget is checked/recorded by the caller (main thread)."""
    tok = os.environ.get("X_BEARER_TOKEN")
    if not tok:
        return []
    n = x_batch_size(cfg)
    query = (f'"{team}" (injury OR injured OR out OR questionable OR lineup OR starting) '
             f'-is:retweet lang:en')
    try:
        r = requests.get("https://api.x.com/2/tweets/search/recent",
                         params={"query": query, "max_results": n, "tweet.fields": "created_at"},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"X error {team}: {e}")
        return []
    data = r.json().get("data", []) or []
    return [{"src": "x", "text": t.get("text", "")} for t in data]


# ---------------------------------------------------------------- scoring

def score_team(team, items):
    pat = re.compile(r"\b(" + "|".join(re.escape(a) for a in team_aliases(team)) + r")\b", re.I)
    mentions, sents, inj = 0, [], 0
    for it in items:
        if not pat.search(it["text"]):
            continue
        mentions += 1
        if _VADER:
            sents.append(_VADER.polarity_scores(it["text"])["compound"])
        if INJURY_RE.search(it["text"]):
            inj += 1
    return {"mentions": mentions, "sent": (sum(sents) / len(sents)) if sents else 0.0, "inj": inj}


def research_games(games, cfg, store):
    """Research every team in `games`; returns {team: score}. Uses cache within TTL."""
    out, todo = {}, []
    for g in games:
        for team in g["mapping"].values():
            cached = store.get_research(team, cfg["research_ttl_min"])
            if cached is not None:
                out[team] = cached
            elif team not in [t for t, _ in todo]:
                todo.append((team, g["series"]))
    if not todo:
        return out
    series_needed = sorted({s for _, s in todo})
    lb = cfg["research_lookback_hours"]
    # X budget: decide up front (main thread) which teams may be queried this run
    x_teams = []
    if cfg.get("x_enabled") and cfg["x_monthly_read_cap"] > 0 and os.environ.get("X_BEARER_TOKEN"):
        room = cfg["x_monthly_read_cap"] - store.x_reads_this_month()
        per = x_batch_size(cfg)
        x_teams = [t for t, _ in todo][:max(0, room // per)]
        if len(x_teams) < len(todo):
            print(f"X read cap: querying {len(x_teams)} of {len(todo)} teams this run")
    with ThreadPoolExecutor(max_workers=8) as ex:
        rss_f = {s: ex.submit(fetch_rss, cfg["rss_feeds"].get(s, []), lb) for s in series_needed}
        red_f = {s: ex.submit(fetch_reddit, cfg["subreddits"].get(s, []), lb) for s in series_needed}
        x_f = {t: ex.submit(fetch_x, t, cfg) for t in x_teams}
        pool = {s: rss_f[s].result() + red_f[s].result() for s in series_needed}
        x_items = {t: f.result() for t, f in x_f.items()}
    n_x = sum(len(v) for v in x_items.values())
    if n_x:
        store.add_x_reads(n_x)
    for team, series in todo:
        sc = score_team(team, pool[series] + x_items.get(team, []))
        store.put_research(team, sc)
        out[team] = sc
    return out


def cached_research(games, cfg, store):
    out = {}
    for g in games:
        for team in g["mapping"].values():
            c = store.get_research(team, cfg["research_ttl_min"])
            if c is not None:
                out[team] = c
    return out
