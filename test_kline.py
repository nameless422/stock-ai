#!/usr/bin/env python3
import sys
sys.path.insert(0, '/root/.openclaw/workspace/stock-ai')

import httpx
import json

stock_code = '000001'
period = 'daily'

if stock_code.startswith("6"):
    symbol = f"sh{stock_code}"
elif stock_code.startswith("0") or stock_code.startswith("3"):
    symbol = f"sz{stock_code}"
else:
    symbol = f"bj{stock_code}"

period_map = {
    "daily": ("day", "qfqday"),
    "weekly": ("week", "qfqweek"),
    "monthly": ("month", "qfqmonth")
}
period_key, qfq_key = period_map.get(period, ("day", "qfqday"))

url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_{period_key}qfq&param={symbol},{period_key},,,,320,qfq&r=0.1"

print(f"URL: {url}")

try:
    with httpx.Client(timeout=15) as client:
        resp = client.get(url)
        text = resp.text

    print(f"Response length: {len(text)}")
    print(f"Response preview: {text[:200]}")

    json_start = text.find('=') + 1
    data = json.loads(text[json_start:])

    print(f"Code: {data.get('code')}")
    stock_data = data.get("data", {}).get(symbol, {})
    print(f"Stock data keys: {list(stock_data.keys())}")
    klines = stock_data.get(qfq_key, [])
    print(f"K-lines count: {len(klines)}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()