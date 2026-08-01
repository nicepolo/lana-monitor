"""Persistent manual-position guidance; never submits an exchange order.

v2 新增：動能衰退提早止盈/止損偵測
- RSI 超買/超賣反轉警告
- 量能萎縮（vol_ratio 驟降）警告  
- 價格跌破 MA7 警告（多單）/ 突破 MA7 警告（空單）
- 三個條件同時出現 → 升級為「建議平倉」
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


_LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    "add_fraction": 0.25,
    "add_at_r": 0.5,
    "add_min_direction_score": 75,
    "trailing_pct": 0.02,
    "heartbeat_minutes": 60,
    # ── 動能衰退參數 ──
    "momentum_rsi_overbought": 72,       # 多單 RSI 超過此值視為超買風險
    "momentum_rsi_oversold": 28,         # 空單 RSI 低於此值視為超賣風險
    "momentum_vol_ratio_weak": 0.5,      # 量能低於此倍數視為動能萎縮
    "momentum_exit_score_drop": 20,      # lana_score 比開倉時下降此分數視為動能衰退
}


def _now(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def new_store(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    if settings:
        merged.update(settings)
    return {"version": 1, "settings": merged, "positions": [], "events": []}


def load_store(paths: Iterable[str], settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _LOCK:
        for path in paths:
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        store = json.load(handle)
                    store.setdefault("positions", [])
                    store.setdefault("events", [])
                    merged = dict(DEFAULT_SETTINGS)
                    merged.update(store.get("settings", {}))
                    if settings:
                        merged.update(settings)
                    store["settings"] = merged
                    return store
            except (OSError, ValueError, TypeError):
                continue
        return new_store(settings)


def save_store(store: Dict[str, Any], paths: Iterable[str]) -> str:
    last_error: Optional[Exception] = None
    with _LOCK:
        for path in paths:
            temp_path = f"{path}.tmp"
            try:
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(store, handle, ensure_ascii=False, indent=2)
                os.replace(temp_path, path)
                return path
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
        raise OSError(f"Unable to persist position store: {last_error}")


def open_position(
    store: Dict[str, Any], signal: Dict[str, Any], now: Optional[datetime] = None
) -> Tuple[Optional[Dict[str, Any]], str]:
    direction = str(signal.get("direction") or "WATCH").upper()
    coin = str(signal.get("coin") or "").upper()
    signal_id = str(signal.get("signal_id") or "")
    if direction not in ("LONG", "SHORT") or not coin or not signal_id:
        return None, "invalid_signal"

    active = [p for p in store["positions"] if p.get("status") == "ACTIVE"]
    existing = next((p for p in active if p.get("signal_id") == signal_id), None)
    if existing:
        return existing, "already_tracking"
    if any(p.get("coin") == coin for p in active):
        return None, "coin_already_tracking"

    try:
        signal_entry = float(signal.get("entry_zone") or 0)
        entry = float(signal.get("actual_entry_price") or signal_entry)
        stop = float(signal.get("stop_loss") or 0)
        target_1 = float(signal.get("target_1") or 0)
        target_2 = float(signal.get("target_2") or 0)
    except (TypeError, ValueError):
        return None, "invalid_levels"
    if min(entry, stop, target_1, target_2) <= 0:
        return None, "invalid_levels"
    if (direction == "LONG" and not (stop < entry < target_1 < target_2)) or (
        direction == "SHORT" and not (stop > entry > target_1 > target_2)
    ):
        return None, "invalid_levels"

    timestamp = _now(now)
    position = {
        "position_id": uuid.uuid4().hex[:16],
        "signal_id": signal_id,
        "strategy_version": signal.get("strategy_version"),
        "coin": coin,
        "exchange": str(signal.get("exchange") or "BINANCE").upper(),
        "direction": direction,
        "status": "ACTIVE",
        "opened_at": timestamp,
        "closed_at": None,
        "entry_price": entry,
        "signal_entry_price": signal_entry,
        "entry_source": "button_live" if signal.get("actual_entry_price") else "signal",
        "initial_stop": stop,
        "stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "initial_risk": abs(entry - stop),
        "high_water": entry,
        "low_water": entry,
        "last_price": entry,
        "last_r": 0.0,
        "last_heartbeat_at": timestamp,
        "entry_lana_score": 0,          # 開倉時的 lana_score，用來偵測分數下滑
        "add_alerted": False,
        "tp1_alerted": False,
        "tp2_alerted": False,
        "exit_alerted": False,
        "direction_alerted": False,
        "momentum_weak_alerted": False,  # 動能衰退警告（減倉）
        "momentum_exit_alerted": False,  # 動能衰退嚴重（建議平倉）
    }
    store["positions"].insert(0, position)
    store["events"].insert(0, {
        "ts": timestamp, "position_id": position["position_id"],
        "type": "TRACKING_STARTED", "price": entry,
    })
    del store["events"][3000:]
    return position, "tracking_started"


def update_entry(
    store: Dict[str, Any], coin: str, entry_price: float,
    exchange: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    coin = str(coin or "").upper()
    position = next(
        (p for p in store["positions"] if p.get("status") == "ACTIVE" and p.get("coin") == coin),
        None,
    )
    if not position:
        return None, "not_found"
    try:
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        return None, "invalid_entry"
    if entry_price <= 0:
        return None, "invalid_entry"
    stop = float(position.get("initial_stop") or 0)
    direction = position["direction"]
    if (direction == "LONG" and entry_price <= stop) or (direction == "SHORT" and entry_price >= stop):
        return None, "entry_beyond_stop"
    position["entry_price"] = entry_price
    position["entry_source"] = "user_reported"
    position["initial_risk"] = abs(entry_price - stop)
    position["high_water"] = max(entry_price, float(position.get("last_price") or entry_price))
    position["low_water"] = min(entry_price, float(position.get("last_price") or entry_price))
    if exchange:
        position["exchange"] = str(exchange).upper()
    store["events"].insert(0, {
        "ts": _now(), "position_id": position["position_id"],
        "type": "ENTRY_CALIBRATED", "price": entry_price,
        "exchange": position.get("exchange"),
    })
    return position, "updated"


def close_position(
    store: Dict[str, Any], position_id: str, reason: str = "manual",
    now: Optional[datetime] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    position = next((p for p in store["positions"] if p.get("position_id") == position_id), None)
    if not position:
        return None, "not_found"
    if position.get("status") != "ACTIVE":
        return position, "already_closed"
    position["status"] = "CLOSED"
    position["closed_at"] = _now(now)
    position["close_reason"] = reason
    store["events"].insert(0, {
        "ts": position["closed_at"], "position_id": position_id,
        "type": "TRACKING_CLOSED", "reason": reason,
        "price": position.get("last_price"),
    })
    return position, "closed"


def _is_reached(direction: str, price: float, level: float, upward_for_long: bool = True) -> bool:
    if direction == "LONG":
        return price >= level if upward_for_long else price <= level
    return price <= level if upward_for_long else price >= level


def _alert(position: Dict[str, Any], action: str, message: str, now: datetime) -> Dict[str, Any]:
    return {
        "alert_id": uuid.uuid4().hex[:16],
        "ts": _now(now),
        "position_id": position["position_id"],
        "coin": position["coin"],
        "direction": position["direction"],
        "exchange": position.get("exchange", "BINANCE"),
        "action": action,
        "message": message,
        "price": position["last_price"],
        "price_source": position.get("price_source"),
        "price_ts": position.get("price_ts"),
        "r_multiple": position["last_r"],
        "pnl_pct": position["pnl_pct"],
        "stop_loss": position["stop_loss"],
        "target_1": position["target_1"],
        "target_2": position["target_2"],
    }


def _check_momentum_decay(
    position: Dict[str, Any],
    snap: Dict[str, Any],
    settings: Dict[str, Any],
    now: datetime,
    alerts: List[Dict[str, Any]],
):
    """偵測動能衰退，發出減倉或平倉警告。"""
    # 已有止損/止盈觸發就不重複
    if position.get("exit_alerted") or position.get("tp2_alerted"):
        return

    direction = position["direction"]
    is_long = direction == "LONG"

    rsi = float(snap.get("rsi") or 50)
    vol_ratio = float(snap.get("vol_ratio") or 1.0)
    ma7 = float(snap.get("ma7") or 0)
    price = float(position["last_price"])
    lana_score = float(snap.get("lana_score") or 0)
    entry_score = float(position.get("entry_lana_score") or 0)

    # 更新開倉分數（第一次 monitor 時記錄）
    if entry_score == 0 and lana_score > 0:
        position["entry_lana_score"] = lana_score
        entry_score = lana_score

    # ── 三個動能衰退訊號 ──
    signal_rsi = False
    signal_vol = False
    signal_ma = False
    signal_score = False

    if is_long:
        # 多單：RSI 超買後開始下滑（超過72）
        signal_rsi = rsi >= float(settings["momentum_rsi_overbought"])
        # 多單：價格跌破 MA7
        signal_ma = (ma7 > 0) and (price < ma7)
    else:
        # 空單：RSI 超賣（低於28）
        signal_rsi = rsi <= float(settings["momentum_rsi_oversold"])
        # 空單：價格突破 MA7
        signal_ma = (ma7 > 0) and (price > ma7)

    # 量能萎縮（多空通用）
    signal_vol = vol_ratio <= float(settings["momentum_vol_ratio_weak"])

    # 分數大幅下滑
    if entry_score > 0 and lana_score > 0:
        signal_score = (entry_score - lana_score) >= float(settings["momentum_exit_score_drop"])

    # 計算觸發數量
    triggered = sum([signal_rsi, signal_vol, signal_ma, signal_score])

    if triggered >= 3 and not position.get("momentum_exit_alerted"):
        # 三個以上 → 建議平倉
        position["momentum_exit_alerted"] = True
        reasons = []
        if signal_rsi:
            reasons.append(f"RSI={rsi:.0f}({'超買' if is_long else '超賣'})")
        if signal_vol:
            reasons.append(f"量能萎縮({vol_ratio:.1f}x)")
        if signal_ma:
            reasons.append(f"價格{'跌破' if is_long else '突破'}MA7")
        if signal_score:
            reasons.append(f"LANA分下滑{entry_score-lana_score:.0f}分")
        msg = f"🚨 動能多項衰退：{'、'.join(reasons)}，建議提早平倉鎖利（或止損）。"
        alerts.append(_alert(position, "CLOSE", msg, now))

    elif triggered >= 2 and not position.get("momentum_weak_alerted"):
        # 兩個 → 警告減倉
        position["momentum_weak_alerted"] = True
        reasons = []
        if signal_rsi:
            reasons.append(f"RSI={rsi:.0f}")
        if signal_vol:
            reasons.append(f"量能{vol_ratio:.1f}x")
        if signal_ma:
            reasons.append("跌破MA7" if is_long else "突破MA7")
        if signal_score:
            reasons.append(f"分數-{entry_score-lana_score:.0f}")
        msg = f"⚠️ 動能衰退警訊：{'、'.join(reasons)}，建議減倉 30-50% 或提高止損。"
        alerts.append(_alert(position, "REDUCE_50", msg, now))


def monitor_positions(
    store: Dict[str, Any], snapshots: Dict[str, Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Evaluate active positions and emit idempotent guidance alerts."""
    now = now or datetime.now(timezone.utc)
    settings = store["settings"]
    alerts: List[Dict[str, Any]] = []
    changed = False

    for position in store["positions"]:
        if position.get("status") != "ACTIVE":
            continue
        snap = snapshots.get(position["coin"]) or {}
        try:
            price = float(snap.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        changed = True
        direction = position["direction"]
        is_long = direction == "LONG"
        risk = float(position["initial_risk"])
        signed_move = price - position["entry_price"] if is_long else position["entry_price"] - price
        position["last_price"] = price
        position["price_source"] = snap.get("price_source") or position.get("exchange", "BINANCE")
        position["price_ts"] = snap.get("price_ts")
        position["last_r"] = round(signed_move / risk, 3) if risk else 0.0
        position["pnl_pct"] = round(signed_move / position["entry_price"] * 100, 3)
        position["high_water"] = max(float(position["high_water"]), price)
        position["low_water"] = min(float(position["low_water"]), price)

        # ── 止損觸發 ──
        stop_hit = _is_reached(direction, price, float(position["stop_loss"]), upward_for_long=False)
        if stop_hit and not position["exit_alerted"]:
            position["exit_alerted"] = True
            alerts.append(_alert(position, "CLOSE", "價格已觸及止損，建議立即平倉。", now))
            continue
        if stop_hit and position["exit_alerted"]:
            continue

        # ── 止盈觸發 ──
        tp1_hit = _is_reached(direction, price, float(position["target_1"]))
        if tp1_hit and not position["tp1_alerted"]:
            position["tp1_alerted"] = True
            position["stop_loss"] = position["entry_price"]
            alerts.append(_alert(position, "REDUCE_50", "已到目標 1：建議減倉 50%，止損移到成本。", now))

        tp2_hit = _is_reached(direction, price, float(position["target_2"]))
        if tp2_hit and not position["tp2_alerted"]:
            position["tp2_alerted"] = True
            alerts.append(_alert(position, "REDUCE_30", "已到目標 2：建議再減倉 30%，其餘使用移動止損。", now))

        # ── 移動止損（TP1 後啟動）──
        if position["tp1_alerted"] and not position["exit_alerted"]:
            trailing_pct = float(settings["trailing_pct"])
            trailing_stop = (
                position["high_water"] * (1 - trailing_pct)
                if is_long else position["low_water"] * (1 + trailing_pct)
            )
            if is_long:
                position["stop_loss"] = max(float(position["stop_loss"]), trailing_stop)
            else:
                position["stop_loss"] = min(float(position["stop_loss"]), trailing_stop)

        # ── 動能衰退偵測（v2 新增）──
        _check_momentum_decay(position, snap, settings, now, alerts)

        # ── 技術方向反轉 ──
        opposite = str(snap.get("rule_direction") or "WATCH").upper()
        opposite_score = float(snap.get("direction_score") or 0)
        if opposite in ("LONG", "SHORT") and opposite != direction and opposite_score >= 75:
            if not position["direction_alerted"]:
                position["direction_alerted"] = True
                alerts.append(_alert(position, "REDUCE_OR_CLOSE", "技術方向已反轉，建議減倉或平倉。", now))

        # ── 加倉機會 ──
        same_direction = str(snap.get("rule_direction") or "WATCH").upper() == direction
        direction_score = float(snap.get("direction_score") or 0)
        if (
            not position["add_alerted"] and not position["tp1_alerted"]
            and position["last_r"] >= float(settings["add_at_r"])
            and same_direction and direction_score >= float(settings["add_min_direction_score"])
        ):
            position["add_alerted"] = True
            fraction = int(float(settings["add_fraction"]) * 100)
            alerts.append(_alert(
                position, "ADD", f"已有獲利且方向仍一致，可考慮加倉 {fraction}%；不可在虧損時攤平。", now
            ))

        # ── 心跳（無動作時定時回報）──
        last_heartbeat = datetime.fromisoformat(position["last_heartbeat_at"])
        minutes = (now - last_heartbeat).total_seconds() / 60
        if not alerts or alerts[-1].get("position_id") != position["position_id"]:
            if minutes >= float(settings["heartbeat_minutes"]):
                position["last_heartbeat_at"] = _now(now)
                alerts.append(_alert(position, "HOLD", "尚未出現加減倉或平倉條件，依原計畫續抱。", now))

    for item in alerts:
        store["events"].insert(0, dict(item, type="GUIDANCE_ALERT"))
    del store["events"][3000:]
    return alerts, changed or bool(alerts)
