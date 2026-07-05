"""Deterministic direction, signal identity, and trade-level helpers for LANA.

The LLM may explain or veto a setup, but it is never allowed to reverse the
direction selected from the frozen market snapshot in this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict


STRATEGY_VERSION = "lana-direction-v1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _canonical_features(features: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only stable strategy inputs and round floats for repeatable hashes."""
    keys = (
        "coin", "timeframe", "candle_close_ts", "price", "change_24h",
        "ma7", "ma30", "ma120", "rsi", "vol_ratio", "bb_position",
        "funding_rate", "oi_change_24h", "atr", "recent_high", "recent_low",
    )
    stable: Dict[str, Any] = {}
    for key in keys:
        value = features.get(key)
        if isinstance(value, float):
            value = round(value, 10)
        stable[key] = value
    return stable


def feature_hash(features: Dict[str, Any]) -> str:
    raw = json.dumps(
        _canonical_features(features), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def make_signal_id(features: Dict[str, Any]) -> str:
    coin = str(features.get("coin", "UNKNOWN")).upper()
    timeframe = str(features.get("timeframe", "1h"))
    candle_ts = str(features.get("candle_close_ts") or "unknown")
    # One strategy decision per coin/candle. Live derivatives may change inside the
    # hour, so feature_hash is stored for audit but must not create another signal.
    seed = f"{STRATEGY_VERSION}|{coin}|{timeframe}|{candle_ts}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def score_direction(features: Dict[str, Any]) -> Dict[str, Any]:
    """Return symmetric LONG/SHORT scores from a frozen feature snapshot."""
    long_score = 0.0
    short_score = 0.0
    long_reasons = []
    short_reasons = []

    ma7 = _number(features.get("ma7"))
    ma30 = _number(features.get("ma30"))
    ma120 = _number(features.get("ma120"))
    if ma7 and ma30 and ma120:
        if ma7 > ma30 > ma120:
            long_score += 35
            long_reasons.append("MA 多頭排列")
        elif ma7 < ma30 < ma120:
            short_score += 35
            short_reasons.append("MA 空頭排列")
        elif ma7 > ma30:
            long_score += 22
            long_reasons.append("短均線偏多")
        elif ma7 < ma30:
            short_score += 22
            short_reasons.append("短均線偏空")
    elif ma7 and ma30:
        if ma7 > ma30:
            long_score += 18
            long_reasons.append("短均線偏多（長均線不足）")
        elif ma7 < ma30:
            short_score += 18
            short_reasons.append("短均線偏空（長均線不足）")

    change = _number(features.get("change_24h"))
    momentum_points = (
        20 if abs(change) >= 15 else
        16 if abs(change) >= 8 else
        12 if abs(change) >= 4 else
        8 if abs(change) >= 2 else 2
    )
    if change > 0:
        long_score += momentum_points
        long_reasons.append(f"24H 正動能 {change:+.1f}%")
    elif change < 0:
        short_score += momentum_points
        short_reasons.append(f"24H 負動能 {change:+.1f}%")

    rsi = _number(features.get("rsi"), 50.0)
    if 50 <= rsi < 68:
        long_score += 15
        long_reasons.append("RSI 多方健康區")
    elif 45 <= rsi < 50 or 68 <= rsi < 75:
        long_score += 8
    elif 25 <= rsi < 35:
        long_score += 5
        long_reasons.append("RSI 超賣反彈候選")

    if 32 < rsi <= 50:
        short_score += 15
        short_reasons.append("RSI 空方健康區")
    elif 25 < rsi <= 32 or 50 < rsi <= 55:
        short_score += 8
    elif 70 < rsi <= 80:
        short_score += 5
        short_reasons.append("RSI 超買回落候選")

    volume_ratio = _number(features.get("vol_ratio"), 1.0)
    volume_points = (
        15 if volume_ratio >= 2.0 else
        12 if volume_ratio >= 1.5 else
        8 if volume_ratio >= 1.0 else
        3 if volume_ratio >= 0.5 else 0
    )
    long_score += volume_points
    short_score += volume_points
    if volume_points >= 12:
        long_reasons.append("量能確認")
        short_reasons.append("量能確認")

    bb_position = str(features.get("bb_position") or "middle")
    long_score += {
        "upper_half": 10, "above_upper": 4,
        "lower_half": 5, "below_lower": 2,
    }.get(bb_position, 5)
    short_score += {
        "lower_half": 10, "below_lower": 4,
        "upper_half": 5, "above_upper": 2,
    }.get(bb_position, 5)

    oi_change = features.get("oi_change_24h")
    if oi_change is not None:
        oi_points = 5 if _number(oi_change) >= 20 else 3 if _number(oi_change) >= 5 else 0
        long_score += oi_points
        short_score += oi_points

    # analyze_coin exposes funding as a percentage, e.g. 0.01 means 0.01%.
    funding = _number(features.get("funding_rate"))
    if funding >= 0.10:
        long_score -= 5
        short_score += 3
        short_reasons.append("多方資金費率擁擠")
    elif funding <= -0.05:
        long_score += 3
        short_score -= 5
        long_reasons.append("空方資金費率擁擠")

    long_score = int(max(0, min(100, round(long_score))))
    short_score = int(max(0, min(100, round(short_score))))
    best = max(long_score, short_score)
    gap = abs(long_score - short_score)
    direction = "LONG" if long_score > short_score else "SHORT"
    veto_reason = None

    if best < 55:
        direction = "WATCH"
        veto_reason = "方向強度不足"
    elif gap < 12:
        direction = "WATCH"
        veto_reason = "多空分數過於接近"
    elif direction == "LONG" and rsi >= 80:
        direction = "WATCH"
        veto_reason = "LONG 過度超買"
    elif direction == "SHORT" and rsi <= 20:
        direction = "WATCH"
        veto_reason = "SHORT 過度超賣"
    elif volume_ratio < 0.5:
        direction = "WATCH"
        veto_reason = "量能不足"

    selected_score = best if direction != "WATCH" else best
    confidence = "高" if best >= 75 and gap >= 20 else "中" if best >= 60 else "低"
    reasons = long_reasons if direction == "LONG" else short_reasons if direction == "SHORT" else []

    result = {
        "direction": direction,
        "long_score": long_score,
        "short_score": short_score,
        "selected_score": selected_score,
        "score_gap": gap,
        "confidence": confidence,
        "reasons": reasons,
        "veto_reason": veto_reason,
        "strategy_version": STRATEGY_VERSION,
        "feature_hash": feature_hash(features),
    }
    result["signal_id"] = make_signal_id(features)
    return result


def build_trade_levels(features: Dict[str, Any], direction: str) -> Dict[str, float]:
    """Create deterministic 1R/2R levels using structure and ATR, capped at 5%."""
    price = _number(features.get("price"))
    if price <= 0 or direction not in ("LONG", "SHORT"):
        return {"entry_zone": 0.0, "stop_loss": 0.0, "target_1": 0.0, "target_2": 0.0}

    atr_value = _number(features.get("atr"), price * 0.02)
    recent_low = _number(features.get("recent_low"), price * 0.98)
    recent_high = _number(features.get("recent_high"), price * 1.02)
    min_distance = price * 0.015
    max_distance = price * 0.05

    if direction == "LONG":
        raw_stop = min(price - 1.5 * atr_value, recent_low * 0.998)
        distance = max(min_distance, min(max_distance, price - raw_stop))
        stop = price - distance
        target_1 = price + distance
        target_2 = price + 2 * distance
    else:
        raw_stop = max(price + 1.5 * atr_value, recent_high * 1.002)
        distance = max(min_distance, min(max_distance, raw_stop - price))
        stop = price + distance
        target_1 = price - distance
        target_2 = price - 2 * distance

    return {
        "entry_zone": round(price, 10),
        "stop_loss": round(stop, 10),
        "target_1": round(target_1, 10),
        "target_2": round(target_2, 10),
    }


def arbitrate_ai_result(rule_decision: Dict[str, Any], ai_result: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the rule decision as authority and allow AI only to confirm/veto."""
    result = dict(ai_result or {})
    ai_direction = str(result.get("direction", "WATCH")).upper()
    ai_score = int(max(0, min(100, round(_number(result.get("score"), 50)))))
    rule_direction = rule_decision.get("direction", "WATCH")
    rule_score = int(rule_decision.get("selected_score", 0))

    result["ai_direction"] = ai_direction
    result["ai_score"] = ai_score
    result.update({
        "rule_direction": rule_direction,
        "long_score": rule_decision.get("long_score", 0),
        "short_score": rule_decision.get("short_score", 0),
        "score_gap": rule_decision.get("score_gap", 0),
        "signal_id": rule_decision.get("signal_id"),
        "feature_hash": rule_decision.get("feature_hash"),
        "strategy_version": rule_decision.get("strategy_version", STRATEGY_VERSION),
    })

    if rule_direction == "WATCH":
        result["direction"] = "WATCH"
        result["score"] = rule_score
        result["arbiter_reason"] = rule_decision.get("veto_reason") or "規則未形成方向"
    elif ai_direction != rule_direction:
        result["direction"] = "WATCH"
        result["score"] = min(rule_score, ai_score)
        result["arbiter_reason"] = f"AI {ai_direction} 與規則 {rule_direction} 不一致"
    else:
        result["direction"] = rule_direction
        result["score"] = round(rule_score * 0.7 + ai_score * 0.3)
        result["arbiter_reason"] = "AI 與規則方向一致"

    return result
