"""STEP 4 — Risk agent (PAPER).

Blocks a trade if any limit is hit; otherwise sizes it with fractional Kelly,
capped per trade, by total open exposure, and by contracts available at the ask.
"""
from common import kelly_binary


def check_and_size(sig, cfg, store):
    bankroll, exposure = store.bankroll(cfg)
    if store.today_pnl() <= -cfg["daily_loss_limit_pct"] * cfg["bankroll"]:
        return 0, "daily loss limit hit"
    if store.trades_today() >= cfg["max_trades_per_day"]:
        return 0, "max trades per day reached"
    if store.open_count() >= cfg["max_open_trades"]:
        return 0, "max open trades reached"
    if store.open_on_event(sig["event_ticker"]):
        return 0, "already have a position in this game"
    if bankroll <= 0:
        return 0, "paper bankroll depleted"
    f = kelly_binary(sig["prob"], sig["cost"]) * cfg["kelly_fraction"]
    stake = min(bankroll * f, bankroll * cfg["max_stake_pct"])
    stake = min(stake, max(0.0, bankroll * cfg["max_open_exposure_pct"] - exposure))
    n = min(int(stake // sig["cost"]), int(sig["size"]))
    if n < 1:
        return 0, "size rounds to < 1 contract"
    return n, None


def check_pick(pick, cfg, store):
    """PICK MODE sizing: a fixed-dollar paper stake. Same safety caps as above, no Kelly, no edge."""
    bankroll, exposure = store.bankroll(cfg)
    if store.today_pnl() <= -cfg["daily_loss_limit_pct"] * cfg["bankroll"]:
        return 0, "daily paper loss limit reached"
    if store.trades_today() >= cfg["max_trades_per_day"]:
        return 0, f"daily pick limit ({cfg['max_trades_per_day']}) reached"
    if store.open_count() >= cfg["max_open_trades"]:
        return 0, f"open-pick limit ({cfg['max_open_trades']}) reached"
    if store.open_on_event(pick["event_ticker"]):
        return 0, "already picked"
    if bankroll <= 0:
        return 0, "paper bankroll used up"
    stake = min(cfg["pick_stake_dollars"], bankroll,
                max(0.0, bankroll * cfg["max_open_exposure_pct"] - exposure))
    n = int(stake // pick["cost"])
    if n < 1:
        return 0, "stake too small for one contract"
    return n, None
