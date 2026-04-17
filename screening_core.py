import json
from collections import Counter
from typing import Optional, Protocol
from urllib.request import Request, urlopen

import httpx

from strategy_engine import build_strategy_context, run_strategy_code


class MarketDataSource(Protocol):
    def list_stocks(self) -> list[dict]:
        ...

    def get_daily_klines(self, symbol: str, days: int = 180) -> list:
        ...

    def get_weekly_klines(self, symbol: str) -> list:
        ...


def stock_code_to_symbol(code: str) -> Optional[str]:
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return None


class TencentMarketDataSource:
    name = "tencent"

    def list_stocks(self) -> list[dict]:
        # 新浪列表接口偶尔还能作为兜底，但稳定性不如东方财富。
        stocks = []
        seen = set()
        page = 1
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        }
        base_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1"
            "&node=hs_a&symbol=&_s_r_a=page"
        )

        while True:
            last_error = None
            payload_text = None
            url = base_url.format(page=page)
            for _ in range(3):
                try:
                    req = Request(url, headers=headers)
                    with urlopen(req, timeout=20) as resp:
                        payload_text = resp.read().decode("utf-8")
                    break
                except Exception as exc:
                    last_error = exc
            if payload_text is None:
                raise last_error or RuntimeError("获取股票列表失败")

            data = json.loads(payload_text)
            if not data:
                break

            for item in data:
                symbol = (item.get("symbol") or "").strip()
                code = (item.get("code") or symbol[2:]).strip()
                name = (item.get("name") or "").strip()
                if symbol.startswith(("sh", "sz", "bj")) and len(code) == 6 and name and code not in seen:
                    seen.add(code)
                    stocks.append({"code": code, "name": name})

            page += 1
            if page > 200:
                break
        return stocks

    def get_daily_klines(self, symbol: str, days: int = 180) -> list:
        return self._fetch_klines(symbol, f"day,,,{days},qfq", "qfqday")

    def get_weekly_klines(self, symbol: str) -> list:
        return self._fetch_klines(symbol, "week,,,30,qfq", "qfqweek")

    def _fetch_klines(self, symbol: str, param_suffix: str, data_key: str) -> list:
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_data&param={symbol},{param_suffix}&r=0.1"
            with httpx.Client(timeout=10) as client:
                resp = client.get(url)
                text = resp.text
            json_start = text.find("=") + 1
            data = json.loads(text[json_start:])
            if data.get("code") != 0:
                return []
            stock_data = data.get("data", {}).get(symbol, {})
            return stock_data.get(data_key, [])
        except Exception:
            return []


class EastMoneyMarketDataSource:
    name = "eastmoney"

    def list_stocks(self) -> list[dict]:
        stocks = []
        seen = set()
        page = 1
        page_size = 500
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        params = {
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14",
        }

        with httpx.Client(timeout=20, headers=headers) as client:
            while True:
                response = client.get(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    params={**params, "pn": str(page)},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") or {}
                rows = data.get("diff") or []
                if not rows:
                    break

                for item in rows:
                    code = str(item.get("f12") or "").strip()
                    name = str(item.get("f14") or "").strip()
                    if len(code) == 6 and name and code not in seen:
                        seen.add(code)
                        stocks.append({"code": code, "name": name})

                total = int(data.get("total") or 0)
                if len(stocks) >= total or len(rows) < page_size:
                    break
                page += 1

        return stocks

    def get_daily_klines(self, symbol: str, days: int = 180) -> list:
        return self._fetch_klines(symbol, klt="101", limit=days)

    def get_weekly_klines(self, symbol: str) -> list:
        return self._fetch_klines(symbol, klt="102", limit=30)

    def _fetch_klines(self, symbol: str, klt: str, limit: int) -> list:
        market = self._market_code(symbol)
        if market is None:
            return []
        code = symbol[2:]
        params = {
            "secid": f"{market}.{code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": klt,
            "fqt": "1",
            "end": "20500101",
            "lmt": str(limit),
        }
        try:
            with httpx.Client(
                timeout=15,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/",
                },
            ) as client:
                response = client.get(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
            rows = (payload.get("data") or {}).get("klines") or []
            result = []
            for row in rows:
                parts = str(row).split(",")
                if len(parts) < 6:
                    continue
                result.append(parts[:6])
            return result
        except Exception:
            return []

    def _market_code(self, symbol: str) -> Optional[int]:
        if symbol.startswith("sz"):
            return 0
        if symbol.startswith("sh"):
            return 1
        if symbol.startswith("bj"):
            return 0
        return None


class SwitchingMarketDataSource:
    def __init__(self, sources: Optional[list[MarketDataSource]] = None):
        self.sources = sources or [
            TencentMarketDataSource(),
            EastMoneyMarketDataSource(),
        ]

    def list_stocks(self) -> list[dict]:
        return self._try_sources("list_stocks")

    def get_daily_klines(self, symbol: str, days: int = 180) -> list:
        return self._try_sources("get_daily_klines", symbol, days)

    def get_weekly_klines(self, symbol: str) -> list:
        return self._try_sources("get_weekly_klines", symbol)

    def _try_sources(self, method_name: str, *args) -> list:
        last_error = None
        for source in self.sources:
            try:
                method = getattr(source, method_name)
                rows = method(*args)
                if rows:
                    return rows
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []


class StockScreeningFilter(Protocol):
    def evaluate(self, code: str, name: str) -> dict:
        ...


class StrategyScreeningFilter:
    def __init__(self, data_source: MarketDataSource, target_info: dict):
        self.data_source = data_source
        self.target_info = target_info

    def evaluate(self, code: str, name: str) -> dict:
        result = {
            "code": code,
            "name": name,
            "pass": False,
            "daily": "",
            "weekly": "",
            "reason": "",
            "matched_strategies": [],
            "current_vol": 0,
            "max_vol_3m": 0,
            "dif": 0,
            "dea": 0,
            "score": 0,
            "payload": {},
        }

        try:
            symbol = stock_code_to_symbol(code)
            if not symbol:
                result["reason"] = "不支持的股票代码"
                return result

            daily_klines = self.data_source.get_daily_klines(symbol, 180)
            weekly_klines = self.data_source.get_weekly_klines(symbol)
            context = build_strategy_context(
                {"code": code, "name": name, "symbol": symbol},
                daily_klines,
                weekly_klines,
            )

            strategy_results = []
            for strategy in self.target_info.get("strategies", []):
                strategy_result = run_strategy_code(strategy["code"], context)
                strategy_result["strategy_id"] = strategy["id"]
                strategy_result["strategy_name"] = strategy["name"]
                strategy_results.append(strategy_result)

            if not strategy_results:
                result["reason"] = "没有可用策略"
                return result

            if self.target_info.get("target_type") == "group":
                match_mode = self.target_info.get("target_logic", "AND").upper()
                passed = all(item["pass"] for item in strategy_results) if match_mode == "AND" else any(item["pass"] for item in strategy_results)
            else:
                passed = strategy_results[0]["pass"]

            matched_names = [item["strategy_name"] for item in strategy_results if item["pass"]]
            failed_reasons = [f"{item['strategy_name']}: {item.get('reason', '')}" for item in strategy_results if not item["pass"]]
            pass_reasons = [f"{item['strategy_name']}: {item.get('reason', '')}" for item in strategy_results if item["pass"]]
            daily_snapshot = context["snapshots"]["daily"]

            result["current_vol"] = daily_snapshot.get("current_volume", 0)
            result["max_vol_3m"] = daily_snapshot.get("max_volume_3m", 0)
            result["dif"] = round(daily_snapshot.get("latest_dif", 0), 4)
            result["dea"] = round(daily_snapshot.get("latest_dea", 0), 4)
            result["pass"] = passed
            result["error"] = any(item.get("error") for item in strategy_results)
            result["matched_strategies"] = matched_names
            result["daily"] = "、".join(matched_names) if matched_names else "未命中策略"
            result["weekly"] = " | ".join(pass_reasons if passed else failed_reasons[:3])
            result["reason"] = result["weekly"]
            result["score"] = max([item.get("score", 0) for item in strategy_results] + [0])
            result["payload"] = {
                "target": {
                    "type": self.target_info.get("target_type"),
                    "id": self.target_info.get("target_id"),
                    "name": self.target_info.get("target_name"),
                    "logic": self.target_info.get("target_logic"),
                },
                "strategy_results": strategy_results,
                "snapshots": context["snapshots"],
            }
        except Exception as exc:
            result["error"] = str(exc)
            result["reason"] = str(exc)

        return result


def build_failure_summary(reason_counts: Counter) -> str:
    parts = []
    for reason, count in reason_counts.most_common(5):
        normalized = str(reason or "").strip() or "未命中"
        parts.append(f"{normalized}({count})")
    return "；".join(parts)
