"""
每日选股筛选模块
- 日线：MACD在零轴之上 + 成交额创3个月新高
- 周线：连续2-3根红柱
"""

import httpx
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict
import asyncio

# 所有A股列表（简化）
STOCK_LIST = None


def get_all_stocks() -> List[Dict]:
    """获取所有A股列表"""
    try:
        from urllib.request import Request, urlopen

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
                    with urlopen(req, timeout=30) as resp:
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
    except Exception as e:
        print(f"获取股票列表失败: {e}")
        return []


def get_kline_daily(stock_code: str, days: int = 90) -> Dict:
    """获取日K线数据"""
    try:
        if stock_code.startswith("6"):
            symbol = f"sh{stock_code}"
        elif stock_code.startswith("0") or stock_code.startswith("3"):
            symbol = f"sz{stock_code}"
        elif stock_code.startswith("4") or stock_code.startswith("8") or stock_code.startswith("9"):
            symbol = f"bj{stock_code}"
        else:
            return {"error": "不支持的股票代码"}
        
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={symbol},day,,,{days},qfq&r=0.1"
        
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            text = resp.text
        
        json_start = text.find('=') + 1
        data = json.loads(text[json_start:])
        
        if data.get("code") != 0:
            return {"error": "获取数据失败"}
        
        stock_data = data.get("data", {}).get(symbol, {})
        klines = stock_data.get("qfqday", [])
        
        if not klines:
            return {"error": "无数据"}
        
        return {"klines": klines}
    except Exception as e:
        return {"error": str(e)}


def get_kline_weekly(stock_code: str) -> Dict:
    """获取周K线数据"""
    try:
        if stock_code.startswith("6"):
            symbol = f"sh{stock_code}"
        elif stock_code.startswith("0") or stock_code.startswith("3"):
            symbol = f"sz{stock_code}"
        elif stock_code.startswith("4") or stock_code.startswith("8") or stock_code.startswith("9"):
            symbol = f"bj{stock_code}"
        else:
            return {"error": "不支持的股票代码"}
        
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_weekqfq&param={symbol},week,,,30,qfq&r=0.1"
        
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            text = resp.text
        
        json_start = text.find('=') + 1
        data = json.loads(text[json_start:])
        
        if data.get("code") != 0:
            return {"error": "获取数据失败"}
        
        stock_data = data.get("data", {}).get(symbol, {})
        klines = stock_data.get("qfqweek", [])
        
        if not klines:
            return {"error": "无数据"}
        
        return {"klines": klines}
    except Exception as e:
        return {"error": str(e)}


def check_daily_criteria(klines: List) -> Dict:
    """
    检查日线筛选条件:
    1. MACD在零轴之上（DIF>DEA>0）
    2. 今日成交额 >= 近3个月最高成交额
    """
    if not klines or len(klines) < 60:
        return {"pass": False, "reason": "数据不足"}
    
    # 解析数据
    dates = []
    closes = []
    volumes = []
    
    for item in klines[-90:]:  # 取最近90天
        if len(item) >= 6:
            dates.append(item[0])
            closes.append(float(item[2]))  # 收盘价
            volumes.append(float(item[5]))  # 成交量
    
    if len(closes) < 60:
        return {"pass": False, "reason": "数据不足"}
    
    # 计算MACD
    close_series = pd.Series(closes)
    ema12 = close_series.ewm(span=12).mean()
    ema26 = close_series.ewm(span=26).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9).mean()
    macd_bar = (dif - dea) * 2
    
    # 最新数据
    latest_dif = dif.iloc[-1]
    latest_dea = dea.iloc[-1]
    latest_macd = macd_bar.iloc[-1]
    
    # 条件1: MACD在零轴之上
    macd_above_zero = latest_dif > latest_dea and latest_dif > 0
    
    # 条件2: 成交额创3个月新高（取最近60个交易日）
    recent_volumes = volumes[-60:]
    max_volume_3m = max(recent_volumes[:-1]) if len(recent_volumes) > 1 else recent_volumes[0]
    current_volume = volumes[-1]
    volume_at_high = current_volume >= max_volume_3m
    
    result = {
        "pass": macd_above_zero and volume_at_high,
        "macd_above_zero": macd_above_zero,
        "volume_at_high": volume_at_high,
        "latest_dif": round(latest_dif, 4),
        "latest_dea": round(latest_dea, 4),
        "current_volume": current_volume,
        "max_volume_3m": max_volume_3m,
        "reason": ""
    }
    
    if result["pass"]:
        result["reason"] = f"MACD金叉(零轴上) + 成交额创3月新高"
    elif not macd_above_zero:
        result["reason"] = f"MACD未在零轴之上(DIF={round(latest_dif,2)}, DEA={round(latest_dea,2)})"
    else:
        result["reason"] = f"成交额未创新高({current_volume:.0f} < {max_volume_3m:.0f})"
    
    return result


def check_weekly_criteria(klines: List) -> Dict:
    """
    检查周线筛选条件:
    连续2-3根红柱子（收盘价 > 开盘价）
    """
    if not klines or len(klines) < 3:
        return {"pass": False, "reason": "数据不足"}
    
    # 取最近5周数据
    recent_klines = klines[-5:]
    
    # 检查连续红柱
    red_bars = []
    for item in recent_klines:
        if len(item) >= 6:
            open_price = float(item[1])
            close_price = float(item[2])
            red_bars.append(close_price > open_price)
    
    # 统计连续红柱数量
    consecutive_red = 0
    for is_red in reversed(red_bars):
        if is_red:
            consecutive_red += 1
        else:
            break
    
    result = {
        "pass": 2 <= consecutive_red <= 3,
        "consecutive_red": consecutive_red,
        "recent_red_bars": red_bars[-3:],
        "reason": ""
    }
    
    if result["pass"]:
        result["reason"] = f"周线{consecutive_red}根红柱"
    elif consecutive_red < 2:
        result["reason"] = f"周线连续红柱不足({consecutive_red}根)"
    else:
        result["reason"] = f"周线红柱过多({consecutive_red}根)"
    
    return result


async def screen_stock(stock: Dict) -> Dict:
    """筛选单只股票"""
    code = stock["code"]
    name = stock["name"]
    
    result = {
        "code": code,
        "name": name,
        "daily_pass": False,
        "weekly_pass": False,
        "daily_reason": "",
        "weekly_reason": "",
        "final_pass": False
    }
    
    try:
        # 检查日线
        daily_data = get_kline_daily(code, 90)
        if "error" not in daily_data and daily_data.get("klines"):
            daily_check = check_daily_criteria(daily_data["klines"])
            result["daily_pass"] = daily_check["pass"]
            result["daily_reason"] = daily_check["reason"]
        
        # 检查周线
        weekly_data = get_kline_weekly(code)
        if "error" not in weekly_data and weekly_data.get("klines"):
            weekly_check = check_weekly_criteria(weekly_data["klines"])
            result["weekly_pass"] = weekly_check["pass"]
            result["weekly_reason"] = weekly_check["reason"]
        
        # 最终通过：两个条件都满足
        result["final_pass"] = result["daily_pass"] and result["weekly_pass"]
        
    except Exception as e:
        result["error"] = str(e)
    
    return result


async def run_screening() -> Dict:
    """执行完整筛选"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始选股筛选...")
    
    # 获取所有股票
    stocks = get_all_stocks()
    print(f"共获取 {len(stocks)} 只股票")
    
    if not stocks:
        return {"error": "获取股票列表失败", "results": []}
    
    results = []
    batch_size = 20
    
    for i in range(0, min(len(stocks), 500), batch_size):  # 最多500只演示
        batch = stocks[i:i+batch_size]
        tasks = [screen_stock(s) for s in batch]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for r in batch_results:
            if isinstance(r, Exception):
                continue
            if r.get("final_pass"):
                results.append(r)
                print(f"  ✅ {r['code']} {r['name']} - {r['daily_reason']}, {r['weekly_reason']}")
        
        print(f"  已完成 {min(i+batch_size, len(stocks))}/{min(len(stocks), 500)} 只...")
    
    # 排序：按成交额排序（成交额高的优先）
    results.sort(key=lambda x: x.get("current_volume", 0), reverse=True)
    
    print(f"\n筛选完成！共 {len(results)} 只股票符合条件")
    
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_stocks": len(stocks),
        "matched_count": len(results),
        "results": results
    }


if __name__ == "__main__":
    result = asyncio.run(run_screening())
    print(f"\n最终结果: {json.dumps(result, ensure_ascii=False, indent=2)[:2000]}")
