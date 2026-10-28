# TheEsness Kalshi picks (paper only)

## Pick mode (default)

The bot picks a side in every Kalshi game it can match to sportsbook odds, for fun. No price edge is needed.

**The rule (`picks.py`, `choose_side`):** back the side the sportsbooks give the higher chance to win.

- **Upset coin:** with `pick_upset_rate` (0.25), it takes the underdog instead. The coin is seeded by the game's ticker, so a game's pick never changes between runs.
- **Injury override:** if recent ESPN news has `injury_veto_count` (2) or more injury mentions for the favorite, and more than for the underdog, it takes the underdog (`pick_injury_override`). This is keyword matching, so it can misfire on nicknames.
- **Stake:** each pick is a `pick_stake_dollars` ($10) paper bet at the cheaper Kalshi route (YES on the team, or NO on the opponent).
- **Caps that still apply:** `max_open_trades` (8), `max_trades_per_day` (10), the daily paper loss limit, and one pick per game.
- **Gates that don't apply:** no edge, odds-age, "sportsbooks disagree" or minimum-size gates. Picks use the last consensus available, and only 2 books are needed (`pick_min_books`).
- **Window:** games starting in the next 36h (`max_hours_ahead`). Odds are only bought for sports with a game ending in that window, reused for 3h (`odds_cache_min`), and capped at 15 credits a day, so it stays on the free plan.
- **Learning:** the self-tuning step is off in pick mode. It exists to protect an edge, and would block whole sports after a few losing favorites.
- **Telegram:** one "NEW PICKS" message per run with new picks, and one "RESULTS" message when picks settle.

**Edge mode:** set `"mode": "edge"` in `config.json` to go back to the original edge-hunting pipeline described below.


This bot follows the same five steps as the bot in the video. It **never places real orders**. Every trade is a paper trade logged to `kalshibot.db`.

| Step | What it does |
|---|---|
| **1 Scan** | Pages through all open Kalshi markets and keeps those passing the 24h-volume and time-to-resolution filters. Flags 24h moves ≥ 10¢ and spreads ≥ 10¢. Matches sports game markets to de-vigged sportsbook consensus odds. |
| **2 Research** | Runs in parallel for games near an edge: ESPN RSS, plus Reddit if you have approved access. Scores each team on mentions, VADER sentiment and injury-keyword hits. Results are cached for 60 min. X support exists but is **off by default** because X is paid. |
| **3 Predict** | Uses the sportsbook consensus probability. Once enough games settle, it trains XGBoost starting from the consensus (corrections only). The model only turns on if it beats the consensus on held-out games. When it's on, a trade needs an edge under **both** the model and the consensus (the confidence gate). An injury-news veto blocks trades on teams with fresh injury reports. |
| **4 Risk** | Blocks trades on the daily loss limit, trades-per-day, open-trade cap, or an existing position in the same game. Otherwise sizes with quarter-Kelly, capped per trade and by total exposure. |
| **5 Learn** | After every loss, runs five checks: stale odds, price moved against us before start (CLV), thin edge, research conflict, overconfident probability range. Otherwise the loss is logged as variance. Then it tightens settings when a pattern is actually losing money: raises a sport's min edge, shortens the odds-freshness limit, lowers the injury-veto bar, or blocks a sport. It only tightens, never loosens. Changes are saved to `learned_overrides.json`. |

## Website

Every run rebuilds `docs/index.html`, a public price board, and commits it. GitHub Pages serves it at **https://seed808.github.io/kalshi-bot/**. It shows:

- the biggest current gap between Kalshi and the sportsbooks
- every matched upcoming game, with Kalshi price vs. fair price and edge after fees
- open and settled paper trades
- what the bot learned from losses
- unusual Kalshi markets

One-time setup (repo must be public on GitHub Free): **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/docs` → Save.**

`docs/board.json` holds the same board as data.

## Running on GitHub Actions (no computer needed)

`.github/workflows/bot.yml` runs one full cycle **every hour** in GitHub's cloud and saves `kalshibot.db` (paper trades, training data) back to the repo after each run.

1. **Add secrets.** Go to repo → Settings → Secrets and variables → Actions → New repository secret.
   - `ODDS_API_KEY` (required, free plan)
   - Optional alerts: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (messages are prefixed `[Kalshi bot]`), `NTFY_TOPIC`, `DISCORD_WEBHOOK_URL`
   - Optional research: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`
2. **Allow write access.** Go to repo → Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.
3. **Test it.** Go to Actions → kalshi-bot → **Run workflow**. Open the run and check the summary for the report.
4. **Change settings** by editing `config.json` in the repo. The next run picks it up.

Hourly fits a private repo's 2,000 free Actions minutes/month. Each run is capped at 10 minutes. Don't make it more frequent unless the repo is public, which would expose your paper-trade history.

## Setup (running on your own computer instead)

```bash
pip install -r requirements.txt
cp config.example.json config.json

export ODDS_API_KEY=...             # required — free Starter plan at the-odds-api.com
export REDDIT_CLIENT_ID=...         # optional, free — needs Reddit-approved API access
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT="python:kalshibot:0.2 (by /u/yourname)"
export DISCORD_WEBHOOK_URL=...      # optional alerts
export NTFY_TOPIC=...               # optional phone alerts via the ntfy app
```

Any optional source left unset is skipped. RSS works with no keys.

## Commands

```bash
python main.py series            # list Kalshi Sports series — verify tickers FIRST
python main.py anomalies         # step 1 whole-exchange scan
python main.py scan              # full pipeline, verbose, no paper trades
python main.py paper -v          # one full cycle with paper trades + alerts
python main.py run --interval 1800   # loop every 30 min
python main.py report            # win rate vs expected, ROI, CLV, calibration, causes, learning
python main.py postmortem        # recent loss postmortems
python main.py train             # force model training/evaluation
```

## Staying free (default settings)

Everything the bot uses by default is free: Kalshi market data, ESPN RSS, the Odds API free plan, and Discord/ntfy alerts. Reddit is free if Reddit approves you.

**Odds API free plan = 500 credits/month.** Running out doesn't charge you. Calls just fail until the reset. The bot guards the budget anyway:

- **Monthly budget:** `odds_monthly_credit_budget` = 450, leaving a 50-credit buffer.
- **Reserve:** it also reads the API's own remaining-credits counter and stops at `odds_credit_reserve` (25).
- **Daily pacing:** at most `odds_max_credits_per_day` (15), spread over the rest of the month.
- **Cache:** reuses odds for `odds_cache_min` (45 min) instead of re-buying them.
- **Skips idle sports:** spends no credit on a sport with no games in the time window.

**Trade-off:** with about 15 odds pulls a day, the bot sees fresh odds only a few times a day per sport. If cached odds age past `max_odds_staleness_min` (30), it won't trade on them. That's on purpose, because stale odds are a common way to lose. Run `scan` closer to game times to get the most out of each credit.

**X stays off** (`x_enabled: false`, `x_monthly_read_cap: 0`). It's $0.005 per post read. Turning it on is the only way this bot can cost money.

## Tennis

- **Kalshi series:** `KXATPMATCH` (ATP) and `KXWTAMATCH` (WTA) match winners.
- **Odds API keys:** one per tournament. Each run uses every active `tennis_atp_*` / `tennis_wta_*` key from the free `/v4/sports` list, cached 6h.
- **Not covered:** Challenger events (`KXCHALLENGERMATCH`, `KXWTACHALLENGERMATCH`) and tournaments the Odds API doesn't list have no sportsbook odds, so they're skipped.
- **Matching:** player names are matched with accents removed.
- **Research:** uses full player names only, so siblings (e.g. the Cerundolos) aren't mixed up.
- **Retirements:** Kalshi resolves to the match winner "after a ball has been played". Sportsbooks may void a bet on retirement, so mid-match retirements can make the consensus a slightly imperfect estimate.
- **Credits:** each active tournament costs 1 Odds API credit per refresh. Odds are only bought for a sport with a game or match **ending within `odds_spend_horizon_hours` (12h)**, which keeps every sport inside the free 500/month.

To find series/keys for any other sport (costs no odds credits): **Actions → kalshi-bot → Run workflow → command: discover**, then read `last_run.log`. The default keywords are tennis; locally, `python main.py discover --q golf,pga`.

## Know before trusting it

- **Series tickers.** Only `KXNHLGAME` appeared in the sources I found. Verify the others with `series` and fix `config.json`.
- **Resolution rules.** Read `rules_primary` on a few markets to confirm Kalshi settles like the sportsbook moneyline (overtime, postponements).
- **The model starts OFF.** It needs `model_min_samples` (400) settled market snapshots, about 200 games. In offline testing with a weak signal, it correctly stayed off because it couldn't beat the consensus.
- **Sentiment and injury rules are keyword-based**, not an LLM. Team nicknames can match unrelated posts (e.g. "Heat", "Kings").
- **Paper fills assume you take the ask** and pay the taker fee.
- **Sample size.** Under ~100 settled trades, results are mostly noise. Watch average CLV and the calibration table, not just the win rate.
- **Unblocking a sport.** Delete it from `blocked_series` in `learned_overrides.json`.
