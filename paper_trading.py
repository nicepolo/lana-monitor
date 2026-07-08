"""Small, deterministic paper broker used before any real OKX execution exists."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


_LOCK = threading.RLock()


DEFAULT_SETTINGS = {
    "capital": 800.0,
    "risk_pct": 1.5,
    "fixed_margin": 45.0,
    "min_signal_score": 70,
    "max_open_positions": 3,
    "leverage": 8.0,
    "fee_rate": 0.0005,
    "slippage_rate": 0.0005,
    "trailing_pct": 0.02,
    "max_hold_hours": 24,
    "time_stop_hours": 6,
    "time_stop_min_r": 0.3,
    "stop_cooldown_hours": 8,
    "max_coin_stops_24h": 2,
    "tp1_fraction": 0.4,
    "tp2_fraction": 0.4,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_book(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    if settings:
        merged.update(settings)
    return {"version": 1, "settings": merged, "signals": [], "trades": [], "events": []}


def load_book(paths: Iterable[str], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _LOCK:
        for path in paths:
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        book = json.load(handle)
                    book.setdefault("signals", [])
                    book.setdefault("trades", [])
                    book.setdefault("events", [])
                    merged = dict(DEFAULT_SETTINGS)
                    merged.update(book.get("settings", {}))
                    if settings:
                        merged.update(settings)
                    book["settings"] = merged
                    return book
            except (OSError, ValueError, TypeError):
                continue
        return new_book(settings)


def save_book(book: Dict[str, Any], paths: Iterable[str]) -> str:
    """Atomically persist to the first writable path and return that path."""
    last_error: Optional[Exception] = None
    with _LOCK:
        for path in paths:
            temp_path = f"{path}.tmp"
            try:
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(book, handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, path)
                return path
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
        raise OSError(f"Unable to persist paper book: {last_error}")


def record_signal(book: Dict[str, Any], signal: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    signal_id = signal.get("signal_id")
    if not signal_id:
        raise ValueError("signal_id is required")
    existing = next((item for item in book["signals"] if item.get("signal_id") == signal_id), None)
    if existing:
        return existing, False
    frozen = dict(signal)
    frozen.setdefault("recorded_at", _now())
    book["signals"].insert(0, frozen)
    del book["signals"][1000:]
    return frozen, True


def record_and_open(
    paths: Iterable[str], settings: Dict[str, Any], signal: Dict[str, Any]
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], str, bool]:
    """Atomically record an immutable signal and optionally open its paper trade."""
    path_list = list(paths)
    with _LOCK:
        book = load_book(path_list, settings)
        frozen, created = record_signal(book, signal)
        if created:
            trade, reason = open_trade(book, frozen)
            save_book(book, path_list)
            return book, trade, reason, True
        existing_trade = next(
            (trade for trade in book["trades"] if trade.get("signal_id") == signal.get("signal_id")),
            None,
        )
        return book, existing_trade, "duplicate_signal", False


def open_trade(book: Dict[str, Any], signal: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    settings = book["settings"]
    direction = str(signal.get("direction", "WATCH")).upper()
    score = float(signal.get("score") or 0)
    coin = str(signal.get("coin") or "").upper()
    signal_id = signal.get("signal_id")

    if direction not in ("LONG", "SHORT"):
        return None, "watch_signal"
    if score < float(settings["min_signal_score"]):
        return None, "score_below_minimum"
    if any(t.get("signal_id") == signal_id for t in book["trades"]):
        return None, "duplicate_signal"
    open_trades = [t for t in book["trades"] if t.get("status") == "OPEN"]
    if len(open_trades) >= int(settings["max_open_positions"]):
        return None, "max_open_positions"
    if any(t.get("coin") == coin for t in open_trades):
        return None, "coin_already_open"

    now_dt = datetime.now(timezone.utc)
    recent_coin_stops = []
    for prior in book["trades"]:
        if prior.get("coin") != coin or prior.get("exit_reason") != "STOP_LOSS":
            continue
        try:
            closed_at = datetime.fromisoformat(prior["closed_at"])
            age_hours = (now_dt - closed_at).total_seconds() / 3600
        except (KeyError, TypeError, ValueError):
            continue
        if age_hours < 24:
            recent_coin_stops.append(age_hours)
    if len(recent_coin_stops) >= int(settings.get("max_coin_stops_24h", 2)):
        return None, "coin_stopped_twice_24h"
    if recent_coin_stops and min(recent_coin_stops) < float(settings.get("stop_cooldown_hours", 8)):
        return None, "coin_stop_cooldown"

    entry = float(signal.get("entry_zone") or 0)
    stop = float(signal.get("stop_loss") or 0)
    target_1 = float(signal.get("target_1") or 0)
    target_2 = float(signal.get("target_2") or 0)
    if entry <= 0 or stop <= 0:
        return None, "invalid_levels"
    if (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
        return None, "invalid_stop_side"

    slippage = float(settings["slippage_rate"])
    fill_entry = entry * (1 + slippage if direction == "LONG" else 1 - slippage)
    stop_distance = abs(fill_entry - stop)
    if stop_distance <= 0:
        return None, "zero_stop_distance"

    fixed_margin = float(settings.get("fixed_margin") or 0)
    if fixed_margin > 0:
        notional = fixed_margin * float(settings["leverage"])
        quantity = notional / fill_entry
        risk_amount = quantity * stop_distance
    else:
        risk_amount = float(settings["capital"]) * float(settings["risk_pct"]) / 100
        quantity = risk_amount / stop_distance
        notional = quantity * fill_entry
    now = _now()
    trade = {
        "trade_id": uuid.uuid4().hex[:16],
        "signal_id": signal_id,
        "strategy_version": signal.get("strategy_version"),
        "coin": coin,
        "direction": direction,
        "score": score,
        "status": "OPEN",
        "opened_at": now,
        "closed_at": None,
        "entry_price": round(fill_entry, 10),
        "initial_stop": stop,
        "stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "original_qty": quantity,
        "remaining_qty": quantity,
        "notional": notional,
        "margin": notional / float(settings["leverage"]),
        "risk_amount": risk_amount,
        "realized_pnl": 0.0,
        "fees": 0.0,
        "unrealized_pnl": 0.0,
        "tp1_hit": False,
        "tp2_hit": False,
        "high_water": fill_entry,
        "low_water": fill_entry,
        "exit_reason": None,
    }
    book["trades"].insert(0, trade)
    book["events"].insert(0, {
        "ts": now, "trade_id": trade["trade_id"], "type": "OPEN",
        "price": trade["entry_price"], "qty": quantity,
    })
    del book["events"][3000:]
    return trade, "opened"


def _close_quantity(
    book: Dict[str, Any], trade: Dict[str, Any], quantity: float,
    reference_price: float, reason: str,
) -> None:
    quantity = min(float(trade["remaining_qty"]), max(0.0, quantity))
    if quantity <= 0:
        return
    settings = book["settings"]
    slippage = float(settings["slippage_rate"])
    direction = trade["direction"]
    exit_price = reference_price * (1 - slippage if direction == "LONG" else 1 + slippage)
    gross = (
        (exit_price - trade["entry_price"]) * quantity
        if direction == "LONG"
        else (trade["entry_price"] - exit_price) * quantity
    )
    fees = (trade["entry_price"] * quantity + exit_price * quantity) * float(settings["fee_rate"])
    net = gross - fees
    trade["remaining_qty"] -= quantity
    trade["realized_pnl"] += net
    trade["fees"] += fees
    book["events"].insert(0, {
        "ts": _now(), "trade_id": trade["trade_id"], "type": reason,
        "price": round(exit_price, 10), "qty": quantity, "net_pnl": net,
    })
    if trade["remaining_qty"] <= trade["original_qty"] * 1e-9:
        trade["remaining_qty"] = 0.0
        trade["status"] = "CLOSED"
        trade["closed_at"] = _now()
        trade["exit_reason"] = reason
        trade["unrealized_pnl"] = 0.0


def mark_positions(book: Dict[str, Any], prices: Dict[str, float], now: Optional[datetime] = None) -> int:
    """Mark all open trades and execute deterministic paper exits at configured levels."""
    now = now or datetime.now(timezone.utc)
    changed = 0
    settings = book["settings"]
    for trade in book["trades"]:
        if trade.get("status") != "OPEN":
            continue
        price = float(prices.get(trade["coin"]) or 0)
        if price <= 0:
            continue
        changed += 1
        trade["high_water"] = max(float(trade["high_water"]), price)
        trade["low_water"] = min(float(trade["low_water"]), price)
        is_long = trade["direction"] == "LONG"

        stop_hit = price <= trade["stop_loss"] if is_long else price >= trade["stop_loss"]
        if stop_hit:
            # Gap through the stop is filled at the worse observed price, never at an ideal stop price.
            stop_fill = min(price, trade["stop_loss"]) if is_long else max(price, trade["stop_loss"])
            _close_quantity(book, trade, trade["remaining_qty"], stop_fill, "STOP_LOSS")
            continue

        tp1_hit = price >= trade["target_1"] if is_long else price <= trade["target_1"]
        if not trade["tp1_hit"] and tp1_hit:
            _close_quantity(
                book, trade, trade["original_qty"] * float(settings.get("tp1_fraction", 0.4)),
                trade["target_1"], "TAKE_PROFIT_1",
            )
            trade["tp1_hit"] = True
            fee_buffer = float(settings["fee_rate"]) * 2 + float(settings["slippage_rate"]) * 2
            trade["stop_loss"] = trade["entry_price"] * (1 + fee_buffer if is_long else 1 - fee_buffer)

        tp2_hit = price >= trade["target_2"] if is_long else price <= trade["target_2"]
        if trade["status"] == "OPEN" and not trade["tp2_hit"] and tp2_hit:
            _close_quantity(
                book, trade, trade["original_qty"] * float(settings.get("tp2_fraction", 0.4)),
                trade["target_2"], "TAKE_PROFIT_2",
            )
            trade["tp2_hit"] = True

        opened_at = datetime.fromisoformat(trade["opened_at"])
        age_hours = (now - opened_at).total_seconds() / 3600
        initial_risk = max(float(trade.get("risk_amount") or 0), 1e-12)
        gross_all = (
            (price - trade["entry_price"]) * trade["original_qty"]
            if is_long else (trade["entry_price"] - price) * trade["original_qty"]
        )
        current_r = gross_all / initial_risk
        time_stop_due = (
            age_hours >= float(settings.get("time_stop_hours", 6))
            and current_r < float(settings.get("time_stop_min_r", 0.3))
        )
        hard_stop_due = age_hours >= float(settings["max_hold_hours"])
        if trade["status"] == "OPEN" and (time_stop_due or hard_stop_due):
            _close_quantity(book, trade, trade["remaining_qty"], price, "TIME_STOP")
            continue

        if trade["status"] == "OPEN" and trade["tp1_hit"]:
            trailing_pct = float(settings["trailing_pct"])
            trailing_stop = (
                trade["high_water"] * (1 - trailing_pct)
                if is_long else trade["low_water"] * (1 + trailing_pct)
            )
            trailing_hit = price <= trailing_stop if is_long else price >= trailing_stop
            if trailing_hit:
                trailing_fill = min(price, trailing_stop) if is_long else max(price, trailing_stop)
                _close_quantity(book, trade, trade["remaining_qty"], trailing_fill, "TRAILING_STOP")
                continue

        if trade["status"] == "OPEN":
            gross = (
                (price - trade["entry_price"]) * trade["remaining_qty"]
                if is_long else
                (trade["entry_price"] - price) * trade["remaining_qty"]
            )
            trade["unrealized_pnl"] = gross

    del book["events"][3000:]
    return changed


def book_summary(book: Dict[str, Any]) -> Dict[str, Any]:
    trades: List[Dict[str, Any]] = book.get("trades", [])
    open_trades = [trade for trade in trades if trade.get("status") == "OPEN"]
    closed = [trade for trade in trades if trade.get("status") == "CLOSED"]
    wins = [trade for trade in closed if float(trade.get("realized_pnl") or 0) > 0]
    realized = sum(float(trade.get("realized_pnl") or 0) for trade in trades)
    unrealized = sum(float(trade.get("unrealized_pnl") or 0) for trade in open_trades)
    return {
        "signals": len(book.get("signals", [])),
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "realized_pnl": round(realized, 4),
        "unrealized_pnl": round(unrealized, 4),
    }
