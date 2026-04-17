"""
选股策略引擎
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


DEFAULT_STRATEGY_NAME = "默认强势筛选"
DEFAULT_STRATEGY_DESCRIPTION = "复刻当前项目的默认选股逻辑：日线 MACD 零轴上且成交量创近 3 个月新高，周线连续 2-3 根红柱。"
DEFAULT_STRATEGY_CODE = """
def run_strategy(context):
    daily = context["snapshots"]["daily"]
    weekly = context["snapshots"]["weekly"]
    if not daily.get("enough_data"):
        return {"pass": False, "reason": "日线数据不足"}
    if not weekly.get("enough_data"):
        return {"pass": False, "reason": "周线数据不足"}

    macd_ok = daily["latest_dif"] > daily["latest_dea"] and daily["latest_dif"] > 0
    volume_ok = daily["current_volume"] >= daily["max_volume_3m"] and daily["max_volume_3m"] > 0
    weekly_ok = 2 <= weekly["consecutive_red"] <= 3

    reason_parts = []
    if macd_ok and volume_ok:
        reason_parts.append("MACD零轴上+成交量3月新高")
    else:
        if not macd_ok:
            reason_parts.append(f"MACD未满足(DIF={daily['latest_dif']:.2f}, DEA={daily['latest_dea']:.2f})")
        if not volume_ok:
            reason_parts.append("成交量未创新高")

    if weekly_ok:
        reason_parts.append(f"周线{weekly['consecutive_red']}根红柱")
    else:
        reason_parts.append(f"周线连续红柱={weekly['consecutive_red']}")

    return {
        "pass": macd_ok and volume_ok and weekly_ok,
        "reason": " | ".join(reason_parts),
        "score": 100 if macd_ok and volume_ok and weekly_ok else 0,
        "metrics": {
            "current_volume": daily["current_volume"],
            "max_volume_3m": daily["max_volume_3m"],
            "dif": daily["latest_dif"],
            "dea": daily["latest_dea"],
            "consecutive_red": weekly["consecutive_red"],
        },
    }
""".strip()


STRATEGY_TEMPLATE = """
def run_strategy(context):
    stock = context["stock"]
    daily = context["snapshots"]["daily"]
    weekly = context["snapshots"]["weekly"]
    indicators = context["indicators"]

    if not daily.get("enough_data"):
        return {"pass": False, "reason": "日线数据不足"}

    latest_close = daily["latest_close"]
    ma20 = indicators["daily"]["ma20"][-1] if indicators["daily"]["ma20"] else 0
    passed = latest_close > ma20

    return {
        "pass": passed,
        "reason": f"{stock['name']} 收盘价{'高于' if passed else '低于'} MA20",
        "score": 80 if passed else 20,
        "metrics": {
            "latest_close": latest_close,
            "ma20": ma20,
        },
    }
""".strip()


def _series(values: List[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _macd(close_values: List[float]) -> Dict[str, List[float]]:
    close_series = _series(close_values)
    dif = close_series.ewm(span=12).mean()
    dea = dif.ewm(span=9).mean()
    bar = (dif - dea) * 2
    return {
        "dif": dif.fillna(0).tolist(),
        "dea": dea.fillna(0).tolist(),
        "bar": bar.fillna(0).tolist(),
    }


def _moving_average(values: List[float], window: int) -> List[float]:
    return _series(values).rolling(window=window).mean().fillna(0).tolist()


def _normalize_kline_rows(klines: List[List[Any]], limit: Optional[int] = None) -> Dict[str, List[float]]:
    rows = klines[-limit:] if limit else klines
    dates, opens, closes, highs, lows, volumes = [], [], [], [], [], []
    for item in rows:
        if len(item) < 6:
            continue
        dates.append(str(item[0]))
        opens.append(float(item[1]))
        closes.append(float(item[2]))
        highs.append(float(item[3]))
        lows.append(float(item[4]))
        volumes.append(float(item[5]))
    return {
        "dates": dates,
        "open": opens,
        "close": closes,
        "high": highs,
        "low": lows,
        "volume": volumes,
    }


def _daily_snapshot(klines: List[List[Any]]) -> Dict[str, Any]:
    normalized = _normalize_kline_rows(klines, limit=120)
    closes = normalized["close"]
    volumes = normalized["volume"]
    macd = _macd(closes) if closes else {"dif": [], "dea": [], "bar": []}
    enough_data = len(closes) >= 60
    current_volume = volumes[-1] if volumes else 0
    recent_volumes = volumes[-60:] if len(volumes) >= 60 else volumes
    max_volume_3m = max(recent_volumes[:-1]) if len(recent_volumes) > 1 else (recent_volumes[0] if recent_volumes else 0)
    return {
        "enough_data": enough_data,
        "rows": len(closes),
        "latest_open": normalized["open"][-1] if normalized["open"] else 0,
        "latest_close": closes[-1] if closes else 0,
        "latest_high": normalized["high"][-1] if normalized["high"] else 0,
        "latest_low": normalized["low"][-1] if normalized["low"] else 0,
        "current_volume": current_volume,
        "max_volume_3m": max_volume_3m,
        "latest_dif": macd["dif"][-1] if macd["dif"] else 0,
        "latest_dea": macd["dea"][-1] if macd["dea"] else 0,
        "latest_macd_bar": macd["bar"][-1] if macd["bar"] else 0,
    }


def _weekly_snapshot(klines: List[List[Any]]) -> Dict[str, Any]:
    normalized = _normalize_kline_rows(klines, limit=30)
    red_bars: List[bool] = []
    for open_price, close_price in zip(normalized["open"], normalized["close"]):
        red_bars.append(close_price > open_price)
    consecutive_red = 0
    for is_red in reversed(red_bars):
        if not is_red:
            break
        consecutive_red += 1
    return {
        "enough_data": len(normalized["close"]) >= 3,
        "rows": len(normalized["close"]),
        "latest_open": normalized["open"][-1] if normalized["open"] else 0,
        "latest_close": normalized["close"][-1] if normalized["close"] else 0,
        "consecutive_red": consecutive_red,
        "recent_red_bars": red_bars[-5:],
    }


def build_strategy_context(stock: Dict[str, Any], daily_klines: List[List[Any]], weekly_klines: List[List[Any]]) -> Dict[str, Any]:
    daily = _normalize_kline_rows(daily_klines, limit=180)
    weekly = _normalize_kline_rows(weekly_klines, limit=60)
    daily_macd = _macd(daily["close"]) if daily["close"] else {"dif": [], "dea": [], "bar": []}
    weekly_macd = _macd(weekly["close"]) if weekly["close"] else {"dif": [], "dea": [], "bar": []}
    return {
        "stock": stock,
        "daily_klines": daily_klines,
        "weekly_klines": weekly_klines,
        "data": {
            "daily": daily,
            "weekly": weekly,
        },
        "snapshots": {
            "daily": _daily_snapshot(daily_klines),
            "weekly": _weekly_snapshot(weekly_klines),
        },
        "indicators": {
            "daily": {
                "ma5": _moving_average(daily["close"], 5),
                "ma10": _moving_average(daily["close"], 10),
                "ma20": _moving_average(daily["close"], 20),
                "ma60": _moving_average(daily["close"], 60),
                "macd": daily_macd,
            },
            "weekly": {
                "ma5": _moving_average(weekly["close"], 5),
                "ma10": _moving_average(weekly["close"], 10),
                "ma20": _moving_average(weekly["close"], 20),
                "macd": weekly_macd,
            },
        },
    }


def get_strategy_contract() -> Dict[str, Any]:
    return {
        "inputs": [
            {"name": "stock.code", "type": "string", "description": "股票代码"},
            {"name": "stock.name", "type": "string", "description": "股票名称"},
            {"name": "daily_klines", "type": "list", "description": "原始日线K线，字段顺序为 [日期, 开盘, 收盘, 最高, 最低, 成交量]"},
            {"name": "weekly_klines", "type": "list", "description": "原始周线K线，字段顺序同上"},
            {"name": "data.daily", "type": "object", "description": "归一化后的日线数据数组，含 dates/open/close/high/low/volume"},
            {"name": "data.weekly", "type": "object", "description": "归一化后的周线数据数组"},
            {"name": "snapshots.daily", "type": "object", "description": "常用日线快照，如 latest_close/current_volume/max_volume_3m/latest_dif/latest_dea"},
            {"name": "snapshots.weekly", "type": "object", "description": "常用周线快照，如 consecutive_red"},
            {"name": "indicators.daily", "type": "object", "description": "日线常用技术指标，含 ma5/ma10/ma20/ma60/macd"},
            {"name": "indicators.weekly", "type": "object", "description": "周线常用技术指标"},
        ],
        "output": {
            "pass": "bool，是否命中策略",
            "reason": "string，策略命中或未命中的原因说明",
            "score": "number，可选，用于排序或辅助说明",
            "metrics": "object，可选，附加数值指标，会写入结果详情",
        },
        "template": STRATEGY_TEMPLATE,
    }


SAFE_GLOBALS = {
    "__builtins__": {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "round": round,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "Exception": Exception,
        "TypeError": TypeError,
        "ValueError": ValueError,
        "zip": zip,
    },
    "np": np,
    "pd": pd,
}


def run_strategy_code(code: str, context: Dict[str, Any]) -> Dict[str, Any]:
    local_vars: Dict[str, Any] = {}
    try:
        exec(code, SAFE_GLOBALS.copy(), local_vars)
        func = local_vars.get("run_strategy")
        if not callable(func):
            return {"pass": False, "reason": "策略代码中未定义 run_strategy(context) 函数", "error": True}
        raw_result = func(context)
        if not isinstance(raw_result, dict):
            return {"pass": False, "reason": "策略函数必须返回 dict", "error": True}
        result = dict(raw_result)
        result["pass"] = bool(result.get("pass"))
        result["reason"] = str(result.get("reason", "") or ("命中" if result["pass"] else "未命中"))
        metrics = result.get("metrics")
        if metrics is None:
            result["metrics"] = {}
        elif not isinstance(metrics, dict):
            result["metrics"] = {"value": metrics}
        if "score" in result:
            try:
                result["score"] = float(result["score"])
            except Exception:
                result["score"] = 0
        return result
    except Exception as exc:
        return {
            "pass": False,
            "reason": f"策略执行异常: {exc}",
            "error": True,
            "traceback": traceback.format_exc(limit=3),
        }
