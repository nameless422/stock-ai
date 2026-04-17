from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import httpx
import numpy as np
import pandas as pd

from app.config import has_database_config, settings
from app.repositories.screening_repository import ScreeningRepository
from app.core.screening_core import SwitchingMarketDataSource, stock_code_to_symbol


screening_repository = ScreeningRepository(settings.db_path)
market_cache: dict = {}
market_cache_lock = threading.Lock()
market_sync_lock = threading.Lock()
market_inflight: dict[tuple, asyncio.Future] = {}


def cache_get(cache_key):
    now = time.monotonic()
    with market_cache_lock:
        item = market_cache.get(cache_key)
        if not item:
            return None
        expires_at, payload = item
        if expires_at <= now:
            market_cache.pop(cache_key, None)
            return None
        return payload


def cache_set(cache_key, payload, ttl_seconds: float):
    with market_cache_lock:
        market_cache[cache_key] = (time.monotonic() + ttl_seconds, payload)


async def run_singleflight(cache_key: tuple, producer: Callable[[], Awaitable[Any]]) -> Any:
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    owner = False
    with market_cache_lock:
        future = market_inflight.get(cache_key)
        if future is None:
            future = loop.create_future()
            market_inflight[cache_key] = future
            owner = True

    if not owner:
        return await future

    try:
        result = await producer()
        future.set_result(result)
        return result
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        with market_cache_lock:
            market_inflight.pop(cache_key, None)


def get_market_now() -> datetime:
    return datetime.now(settings.market_tz)


def is_market_trading_day(now: Optional[datetime] = None) -> bool:
    current = (now or get_market_now()).astimezone(settings.market_tz)
    return current.weekday() < 5


def is_market_open(now: Optional[datetime] = None) -> bool:
    current = (now or get_market_now()).astimezone(settings.market_tz)
    if not is_market_trading_day(current):
        return False
    current_minutes = current.hour * 60 + current.minute
    return (9 * 60 + 30) <= current_minutes < (11 * 60 + 30) or (13 * 60) <= current_minutes < (15 * 60)


def next_trading_day_run(hour: int, minute: int, now: Optional[datetime] = None) -> datetime:
    current = (now or get_market_now()).astimezone(settings.market_tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def next_daily_run(hour: int, minute: int, now: Optional[datetime] = None) -> datetime:
    current = (now or get_market_now()).astimezone(settings.market_tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


def _stock_code_to_market_symbol(stock_code: str) -> str:
    stock_code = (stock_code or "").strip().lower()
    if stock_code.startswith(("sh", "sz", "bj")) and len(stock_code) == 8 and stock_code[2:].isdigit():
        return stock_code
    if stock_code.startswith("6"):
        return f"sh{stock_code}"
    if stock_code.startswith(("0", "3")):
        return f"sz{stock_code}"
    return f"bj{stock_code}"


def parse_stock_info_payload(stock_code: str, text: str) -> dict:
    symbol = _stock_code_to_market_symbol(stock_code)
    display_code = symbol if stock_code.startswith(("sh", "sz", "bj")) else stock_code
    start = text.find('"') + 1
    end = text.find('"', start)
    data = text[start:end].split(',')
    if len(data) < 32:
        return {"error": "股票代码不存在或已退市"}
    name = data[0]
    open_price = float(data[1]) if data[1] else 0
    close_prev = float(data[2]) if data[2] else 0
    current = float(data[3]) if data[3] else 0
    high = float(data[4]) if data[4] else 0
    low = float(data[5]) if data[5] else 0
    volume = float(data[8]) if data[8] else 0
    amount = float(data[9]) if data[9] else 0
    change = ((current - close_prev) / close_prev * 100) if close_prev else 0
    return {
        "code": stock_code[-6:] if symbol[2:].isdigit() else stock_code,
        "symbol": symbol,
        "display_code": display_code,
        "name": name if name else "",
        "price": current,
        "change": round(change, 2),
        "change_amount": round(current - close_prev, 2),
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "close_prev": close_prev,
    }


def _fetch_tencent_klines(symbol: str, period: str, bars: Optional[int] = None, adjust: str = "qfq") -> list:
    config = settings.market_period_config.get(period, settings.market_period_config["daily"])
    request_bars = bars or config["default_bars"]
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?_var=kline_{config['period_key']}{adjust}&param={symbol},{config['period_key']},,,{request_bars},{adjust}&r=0.1"
    )
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
        json_start = resp.text.find("=") + 1
        data = json.loads(resp.text[json_start:])
        if data.get("code") != 0:
            return []
        stock_data = data.get("data", {}).get(symbol, {})
        return stock_data.get(config["payload_key"], []) or []
    except Exception:
        return []


def _eastmoney_market_code(symbol: str) -> Optional[int]:
    if symbol.startswith("sz"):
        return 0
    if symbol.startswith("sh"):
        return 1
    if symbol.startswith("bj"):
        return 0
    return None


def _fetch_eastmoney_klines(symbol: str, period: str, bars: Optional[int] = None) -> list:
    config = settings.market_period_config.get(period, settings.market_period_config["daily"])
    market = _eastmoney_market_code(symbol)
    if market is None:
        return []
    params = {
        "secid": f"{market}.{symbol[2:]}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": config["eastmoney_klt"],
        "fqt": "1",
        "end": "20500101",
        "lmt": str(bars or config["default_bars"]),
    }
    try:
        with httpx.Client(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        ) as client:
            response = client.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params)
            response.raise_for_status()
            payload = response.json()
        rows = (payload.get("data") or {}).get("klines") or []
        result = []
        for row in rows:
            parts = str(row).split(",")
            if len(parts) >= 6:
                result.append(parts[:6])
        return result
    except Exception:
        return []


def fetch_remote_klines(stock_code: str, period: str, bars: Optional[int] = None, adjust: str = "qfq") -> list:
    symbol = stock_code_to_symbol(stock_code)
    if not symbol:
        return []
    if period == "daily":
        rows = SwitchingMarketDataSource().get_daily_klines(symbol, bars or settings.market_period_config["daily"]["default_bars"])
        if rows:
            return rows
    elif period == "weekly":
        rows = SwitchingMarketDataSource().get_weekly_klines(symbol)
        if rows:
            return rows[-(bars or settings.market_period_config["weekly"]["default_bars"]):]
    rows = _fetch_tencent_klines(symbol, period, bars, adjust=adjust)
    if rows:
        return rows
    return _fetch_eastmoney_klines(symbol, period, bars)


def get_kline_rows(
    stock_code: str,
    period: str = "daily",
    bars: Optional[int] = None,
    adjust: str = "qfq",
    prefer_remote: Optional[bool] = None,
) -> list:
    symbol = stock_code_to_symbol(stock_code)
    if not symbol:
        return []
    config = settings.market_period_config.get(period, settings.market_period_config["daily"])
    limit = bars or config["default_bars"]
    remote_first = is_market_open() if prefer_remote is None else prefer_remote
    db_enabled = has_database_config()
    if remote_first:
        remote_rows = fetch_remote_klines(stock_code, period, limit, adjust=adjust)
        if remote_rows:
            if db_enabled:
                screening_repository.save_cached_klines(stock_code, symbol, period, remote_rows, adjust=adjust)
            return remote_rows[-limit:]
        if not db_enabled:
            return []
        return screening_repository.load_cached_klines(stock_code, period, adjust=adjust, limit=limit)
    cached_rows = screening_repository.load_cached_klines(stock_code, period, adjust=adjust, limit=limit) if db_enabled else []
    if cached_rows:
        return cached_rows
    remote_rows = fetch_remote_klines(stock_code, period, limit, adjust=adjust)
    if remote_rows:
        if db_enabled:
            screening_repository.save_cached_klines(stock_code, symbol, period, remote_rows, adjust=adjust)
        return remote_rows[-limit:]
    return []


def sync_stock_kline_cache(stock_code: str, periods: Optional[list] = None) -> dict:
    symbol = stock_code_to_symbol(stock_code)
    if not symbol:
        return {"stock_code": stock_code, "updated": 0, "error": "不支持的股票代码"}
    updated = 0
    errors = []
    for period in periods or ["daily", "weekly"]:
        config = settings.market_period_config.get(period)
        if not config:
            continue
        rows = fetch_remote_klines(stock_code, period, config["default_bars"])
        if not rows:
            errors.append(period)
            continue
        screening_repository.save_cached_klines(stock_code, symbol, period, rows)
        updated += 1
    return {"stock_code": stock_code, "updated": updated, "error_periods": errors}


def sync_market_cache_for_all_stocks() -> dict:
    if not market_sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有同步任务在执行"}
    try:
        stocks = SwitchingMarketDataSource().list_stocks()
        if not stocks:
            return {"ok": False, "error": "股票列表为空"}
        total = len(stocks)
        success = partial = failed = 0
        max_workers = min(max(4, settings.screening_max_workers), 16)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(sync_stock_kline_cache, stock["code"], ["daily", "weekly", "monthly"]): stock
                for stock in stocks
            }
            for future in as_completed(future_map):
                try:
                    result = future.result()
                except Exception:
                    failed += 1
                    continue
                updated = result.get("updated", 0)
                if updated >= 3:
                    success += 1
                elif updated > 0:
                    partial += 1
                else:
                    failed += 1
        return {"ok": True, "total": total, "success": success, "partial": partial, "failed": failed}
    finally:
        market_sync_lock.release()


def get_stock_info(stock_code: str) -> dict:
    cache_key = ("stock_info", stock_code)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        symbol = _stock_code_to_market_symbol(stock_code)
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"https://hq.sinajs.cn/list={symbol}", headers={"Referer": "https://finance.sina.com.cn"})
        result = parse_stock_info_payload(stock_code, resp.text)
        cache_set(cache_key, result, settings.stock_info_ttl)
        return result
    except Exception as exc:
        return {"error": str(exc)}


def get_kline_data(stock_code: str, period: str = "daily", adjust: str = "qfq") -> dict:
    cache_key = ("kline", stock_code, period, adjust)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        klines = get_kline_rows(stock_code, period=period, bars=180, adjust=adjust)
        if not klines:
            return {"error": "暂无数据"}
        dates, opens, closes, highs, lows, volumes = [], [], [], [], [], []
        for item in klines[-180:]:
            if len(item) >= 6:
                dates.append(item[0])
                opens.append(float(item[1]))
                closes.append(float(item[2]))
                highs.append(float(item[3]))
                lows.append(float(item[4]))
                volumes.append(float(item[5]))
        result = {
            "dates": dates,
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "amount": [0] * len(dates),
        }
        cache_set(cache_key, result, settings.kline_ttl)
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def get_stock_info_async(stock_code: str, client: httpx.AsyncClient) -> dict:
    cache_key = ("stock_info", stock_code)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    async def producer() -> dict:
        try:
            symbol = _stock_code_to_market_symbol(stock_code)
            resp = await client.get(
                f"https://hq.sinajs.cn/list={symbol}",
                headers={"Referer": "https://finance.sina.com.cn"},
            )
            result = parse_stock_info_payload(stock_code, resp.text)
            cache_set(cache_key, result, settings.stock_info_ttl)
            return result
        except Exception as exc:
            return {"error": str(exc)}

    return await run_singleflight(cache_key, producer)


async def get_kline_data_async(stock_code: str, period: str, adjust: str) -> dict:
    cache_key = ("kline", stock_code, period, adjust)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    async def producer() -> dict:
        try:
            return await asyncio.to_thread(get_kline_data, stock_code, period, adjust)
        except Exception as exc:
            return {"error": str(exc)}

    return await run_singleflight(cache_key, producer)


async def search_stock_async(keyword: str, client: httpx.AsyncClient) -> dict:
    keyword = (keyword or "").strip()
    cache_key = ("search", keyword.lower())
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    async def producer() -> dict:
        try:
            url = f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15,16,17,18,19,110&key={keyword}&limit=10"
            resp = await client.get(url, headers={"Referer": "https://finance.sina.com.cn"})
            start = resp.text.find('"') + 1
            end = resp.text.find('"', start)
            data = resp.text[start:end]
            results = []
            seen_symbols = set()
            for item in data.split(';'):
                parts = item.split(',')
                if len(parts) >= 4:
                    stock_name = (parts[4].strip() if len(parts) > 4 else "") or (parts[6].strip() if len(parts) > 6 else "") or parts[0].strip()
                    stock_code = parts[2].strip()
                    market_symbol = parts[3].strip()
                    if not stock_code and market_symbol:
                        stock_code = market_symbol[-6:]
                    if stock_code and market_symbol and market_symbol not in seen_symbols:
                        seen_symbols.add(market_symbol)
                        results.append({
                            "code": stock_code,
                            "symbol": market_symbol,
                            "display_code": market_symbol if keyword.isdigit() else stock_code,
                            "name": stock_name,
                            "price": 0,
                            "change": 0,
                        })
            payload = {"results": results[:10]}
            cache_set(cache_key, payload, settings.search_ttl)
            return payload
        except Exception as exc:
            return {"error": str(exc), "results": []}

    return await run_singleflight(cache_key, producer)


async def get_quote_bundle_async(stock_code: str, period: str, adjust: str, client: httpx.AsyncClient) -> dict:
    cache_key = ("quote_bundle", stock_code, period, adjust)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    async def producer() -> dict:
        info, kline_data = await asyncio.gather(
            get_stock_info_async(stock_code, client),
            get_kline_data_async(stock_code, period, adjust),
        )
        if info.get("error"):
            return {"error": info.get("error"), "error_source": "stock"}
        if kline_data.get("error"):
            return {"error": kline_data.get("error"), "error_source": "kline"}
        indicators = calculate_indicators(kline_data)
        result = {
            "stock": info,
            "kline": kline_data,
            "indicators": indicators,
            "analysis": ai_analyze(stock_code, info.get("name", stock_code), kline_data, indicators),
        }
        cache_set(cache_key, result, min(settings.stock_info_ttl, settings.kline_ttl))
        return result

    return await run_singleflight(cache_key, producer)


def calculate_indicators(kline_data: dict) -> dict:
    close = np.array(kline_data["close"])
    high = np.array(kline_data["high"])
    low = np.array(kline_data["low"])
    ma5 = pd.Series(close).rolling(window=5).mean().fillna(0).tolist()
    ma10 = pd.Series(close).rolling(window=10).mean().fillna(0).tolist()
    ma20 = pd.Series(close).rolling(window=20).mean().fillna(0).tolist()
    ma60 = pd.Series(close).rolling(window=60).mean().fillna(0).tolist()
    ema12 = pd.Series(close).ewm(span=12).mean().fillna(0).tolist()
    ema26 = pd.Series(close).ewm(span=26).mean().fillna(0).tolist()
    dif = pd.Series(close).ewm(span=12).mean() - pd.Series(close).ewm(span=26).mean()
    dea = dif.ewm(span=9).mean()
    macd_bar = (dif - dea) * 2
    kdj_k = [50.0] * 8
    kdj_d = [50.0] * 8
    for i in range(8, len(close)):
        pv = low[i - 8:i + 1].min()
        ph = high[i - 8:i + 1].max()
        rsv = 50 if ph == pv else (close[i] - pv) / (ph - pv) * 100
        k_val = kdj_k[-1] * 2 / 3 + rsv / 3
        d_val = kdj_d[-1] * 2 / 3 + k_val / 3
        kdj_k.append(k_val)
        kdj_d.append(d_val)
    kdj_j = [k * 3 - d * 2 for k, d in zip(kdj_k, kdj_d)]

    def calc_rsi(period: int) -> list:
        rsi = []
        for i in range(len(close)):
            if i < period:
                rsi.append(50)
            else:
                gains = []
                losses = []
                for j in range(i - period + 1, i + 1):
                    diff = close[j] - close[j - 1]
                    if diff > 0:
                        gains.append(diff)
                    else:
                        losses.append(abs(diff))
                avg_gain = sum(gains) / period if gains else 0
                avg_loss = sum(losses) / period if losses else 0
                rsi.append(100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss)))
        return rsi

    boll_mid_series = pd.Series(close).rolling(window=20).mean().fillna(0)
    boll_std = pd.Series(close).rolling(window=20).std().fillna(0)
    return {
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ema12": ema12,
        "ema26": ema26,
        "macd": {
            "dif": dif.fillna(0).tolist(),
            "dea": dea.fillna(0).tolist(),
            "bar": macd_bar.fillna(0).tolist(),
        },
        "kdj": {"k": kdj_k[-len(close):], "d": kdj_d[-len(close):], "j": kdj_j[-len(close):]},
        "rsi6": calc_rsi(6),
        "rsi12": calc_rsi(12),
        "rsi24": calc_rsi(24),
        "boll_upper": (boll_mid_series + 2 * boll_std).tolist(),
        "boll_mid": boll_mid_series.tolist(),
        "boll_lower": (boll_mid_series - 2 * boll_std).tolist(),
    }


def ai_analyze(stock_code: str, stock_name: str, kline_data: dict, indicators: dict) -> dict:
    try:
        latest_close = kline_data["close"][-1]
        latest_ma5 = indicators["ma5"][-1]
        latest_ma10 = indicators["ma10"][-1]
        latest_ma20 = indicators["ma20"][-1]
        latest_ma60 = indicators["ma60"][-1]
        latest_macd = {key: indicators["macd"][key][-1] for key in ("dif", "dea", "bar")}
        latest_kdj = {key: indicators["kdj"][key][-1] for key in ("k", "d", "j")}
        latest_rsi = {key: indicators[key][-1] for key in ("rsi6", "rsi12", "rsi24")}
        latest_boll = {
            "upper": indicators["boll_upper"][-1],
            "mid": indicators["boll_mid"][-1],
            "lower": indicators["boll_lower"][-1],
        }
        ma_trend = (
            "多头排列"
            if latest_close > latest_ma5 > latest_ma10 > latest_ma20
            else "空头排列"
            if latest_close < latest_ma5 < latest_ma10 < latest_ma20
            else "震荡"
        )
        score = 5
        if latest_close > latest_ma5:
            score += 0.5
        if latest_ma5 > latest_ma10:
            score += 0.5
        if latest_ma10 > latest_ma20:
            score += 0.5
        if latest_ma20 > latest_ma60:
            score += 0.5
        if latest_macd["dif"] > latest_macd["dea"]:
            score += 1
        if latest_macd["bar"] > 0:
            score += 0.5
        if 20 < latest_kdj["k"] < 80:
            score += 0.3
        if latest_kdj["k"] > latest_kdj["d"] and latest_kdj["d"] < 40:
            score += 0.5
        if 30 < latest_rsi["rsi6"] < 70:
            score += 0.3
        if latest_boll["mid"] < latest_close < latest_boll["upper"]:
            score += 0.4
        elif latest_close < latest_boll["mid"]:
            score -= 0.3
        score = min(max(score, 1), 10)
        medium_trend = "上涨" if latest_close > latest_ma60 else "下跌"
        short_trend = "上涨" if latest_ma5 > latest_ma10 and latest_macd["bar"] > 0 else "下跌" if latest_ma5 < latest_ma10 and latest_macd["bar"] < 0 else "震荡"
        advice = "强烈建议" if score >= 7 else "建议" if score >= 6 else "观望" if score >= 4 else "不建议"
        macd_signal = "金叉" if latest_macd["dif"] > latest_macd["dea"] else "死叉"
        kdj_signal = "超买" if latest_kdj["k"] > 80 else "超卖" if latest_kdj["k"] < 20 else "正常"
        rsi_signal = "超买" if latest_rsi["rsi6"] > 70 else "超卖" if latest_rsi["rsi6"] < 30 else "正常"
        return {
            "score": round(score, 1),
            "short_trend": short_trend,
            "medium_trend": medium_trend,
            "advice": advice,
            "reason": f"均线{ma_trend}，MACD{latest_macd['dif']:.2f}{macd_signal}，KDJ {kdj_signal}，RSI {rsi_signal}，综合评分{score:.1f}分",
        }
    except Exception as exc:
        return {
            "score": 5,
            "short_trend": "震荡",
            "medium_trend": "震荡",
            "advice": "观望",
            "reason": f"分析异常: {str(exc)[:50]}",
        }
