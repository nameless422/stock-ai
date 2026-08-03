from __future__ import annotations

import json
from typing import Optional


STOCK_CONSULTING_SKILL_SYSTEM_PROMPT = (
    "你是 A 股股票咨询 skill。你的任务是把行情、技术指标、最近会话和用户问题合成可读的咨询建议。"
    "请参考专业投研 skill 的结构：先给结论倾向，再给技术依据、风险、相关推荐和观察点。"
    "只能基于输入里的行情与指标，不要编造财务、新闻、板块涨跌或实时市场数据。"
    "相关推荐可以是后续可关注的板块、同类标的、指数或技术条件，但必须说明这是观察方向，不是买卖指令。"
    "不要承诺收益，不要给确定性买卖指令；涉及操作时必须提示不构成投资建议。"
    "输出保持简洁，使用中文。"
)

MARKET_RECOMMENDATION_SYSTEM_PROMPT = (
    "你是 A 股市场咨询助手。用户想了解市场、板块或行情环境时，给出正常的市场观察建议。"
    "如果没有输入实时市场数据，不要假装知道当天指数涨跌、板块排名或新闻。"
    "回答应聚焦：市场所处阶段、应该看哪些指标、可以关注哪些方向、风险是什么、下一步怎么提问。"
    "不要创建策略，不要声称已经扫描全市场，不要给确定性收益承诺。"
)

THEME_STOCK_RECOMMENDATION_SYSTEM_PROMPT = (
    "你是 A 股主题股票推荐 skill。用户询问某个产业链、题材或板块相关股票时，"
    "基于输入候选股票给出观察清单。不要声称已经全市场扫描，不要编造候选列表之外的个股。"
    "如果有行情快照，可以引用当前价、涨跌幅、成交额；如果没有行情，不要编造行情。"
    "必须说明这些是主题相关观察标的，不是买卖建议。"
)

THEME_STOCK_RECOMMENDATIONS = {
    "光模块": {
        "theme": "光模块",
        "aliases": ["光模块", "cpo", "硅光", "光通信", "光器件", "800g", "1.6t光模块"],
        "description": "AI 算力网络里的高速光通信链条，主要关注光模块、光器件、光芯片和相关封装测试。",
        "stocks": [
            {"code": "300308", "name": "中际旭创", "reason": "高速光模块龙头之一，和 AI 数据中心光互联相关度高"},
            {"code": "300502", "name": "新易盛", "reason": "高速光模块重要厂商，受益于海外云厂商光模块需求"},
            {"code": "300394", "name": "天孚通信", "reason": "光器件平台型公司，和光模块上游器件配套相关"},
            {"code": "002281", "name": "光迅科技", "reason": "光通信器件和模块厂商，产品覆盖电信和数通场景"},
            {"code": "000988", "name": "华工科技", "reason": "旗下光通信业务覆盖光模块、光器件等方向"},
            {"code": "603083", "name": "剑桥科技", "reason": "光模块与通信设备相关标的，题材弹性较高"},
            {"code": "300548", "name": "博创科技", "reason": "光电子器件和光模块相关公司，关注高速产品进展"},
            {"code": "301205", "name": "联特科技", "reason": "光模块相关次新成长标的，弹性和波动都较高"},
            {"code": "300570", "name": "太辰光", "reason": "光器件和连接器相关，和光通信链条有关"},
            {"code": "688498", "name": "源杰科技", "reason": "光芯片相关标的，偏上游核心器件方向"},
        ],
        "watch_points": [
            "海外 AI 数据中心资本开支和高速光模块订单兑现",
            "800G/1.6T 产品放量节奏、毛利率和客户集中度",
            "板块高估值后的业绩验证风险和短线拥挤度",
        ],
    },
}


def build_stock_consulting_messages(user_text: str, session_context_text: str, bundle: dict) -> list[dict]:
    context = build_stock_skill_context(bundle)
    return [
        {"role": "system", "content": STOCK_CONSULTING_SKILL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户问题：{user_text}\n\n"
                f"最近会话上下文：\n{session_context_text or '无'}\n\n"
                "当前股票咨询 skill 输入：\n"
                f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
                "请按下面结构回答：\n"
                "1. 结论倾向：一句话说明偏强、震荡、偏弱或数据不足。\n"
                "2. 关键依据：列出 3 条以内，必须对应输入数据。\n"
                "3. 主要风险：列出 2 条以内。\n"
                "4. 相关推荐：给出 2-4 个后续观察方向，例如同板块/相关指数/类似技术形态/需要补充的数据。\n"
                "5. 接下来观察点：给出具体价位或指标条件；没有价位依据时不要编造。\n"
            ),
        },
    ]


def match_theme_stock_recommendation(text: str) -> Optional[dict]:
    content = (text or "").lower().replace(" ", "")
    for payload in THEME_STOCK_RECOMMENDATIONS.values():
        aliases = payload.get("aliases") or []
        if any(str(alias).lower().replace(" ", "") in content for alias in aliases):
            return payload
    return None


def build_theme_stock_recommendation_messages(user_text: str, session_context_text: str, theme_payload: dict) -> list[dict]:
    return [
        {"role": "system", "content": THEME_STOCK_RECOMMENDATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户问题：{user_text}\n\n"
                f"最近会话上下文：\n{session_context_text or '无'}\n\n"
                "主题股票候选：\n"
                f"{json.dumps(theme_payload, ensure_ascii=False, indent=2)}\n\n"
                "请按下面结构回答：\n"
                "1. 主题结论：说明这个主题主要看什么产业链环节。\n"
                "2. 相关股票：按相关度列出 6-10 个，格式为「名称（代码）：观察理由；行情摘要」。\n"
                "3. 怎么筛：给出 3 个筛选标准，例如业绩兑现、订单、估值/位置。\n"
                "4. 风险提示：说明题材波动和不构成投资建议。\n"
            ),
        },
    ]


def build_market_recommendation_messages(user_text: str, session_context_text: str) -> list[dict]:
    return [
        {"role": "system", "content": MARKET_RECOMMENDATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户问题：{user_text}\n\n"
                f"最近会话上下文：\n{session_context_text or '无'}\n\n"
                "请给出市场咨询回复。结构为：市场判断框架、建议关注方向、风险提示、可继续追问的问题。"
                "如果用户只是泛泛问市场机会，不要直接推荐具体股票；优先推荐观察指数、成交量、主线板块和风险偏好。"
            ),
        },
    ]


def format_theme_stock_recommendation_fallback(theme_payload: dict) -> str:
    theme = theme_payload.get("theme") or "相关主题"
    lines = [
        f"{theme}相关股票可以先按产业链位置做观察：",
    ]
    for item in (theme_payload.get("stocks") or [])[:8]:
        quote = item.get("quote") or {}
        quote_text = ""
        if quote and not quote.get("error"):
            price = quote.get("price")
            change = quote.get("change")
            quote_text = f"；当前价 {price}，涨跌幅 {change}"
        lines.append(f"- {item.get('name')}（{item.get('code')}）：{item.get('reason')}{quote_text}")
    watch_points = theme_payload.get("watch_points") or []
    if watch_points:
        lines.append("后续重点看：" + "；".join(watch_points[:3]) + "。")
    lines.append("这些只是主题相关观察标的，不构成投资建议。")
    return "\n".join(lines)


def build_stock_skill_context(bundle: dict) -> dict:
    stock = bundle.get("stock") or {}
    kline = bundle.get("kline") or {}
    indicators = bundle.get("indicators") or {}
    analysis = bundle.get("analysis") or {}

    close = _number_list(kline.get("close"))
    high = _number_list(kline.get("high"))
    low = _number_list(kline.get("low"))
    volume = _number_list(kline.get("volume"))

    latest_close = _last(close)
    latest_high = _last(high)
    latest_low = _last(low)
    latest_volume = _last(volume)
    support_20 = min(low[-20:]) if len(low) >= 20 else None
    resistance_20 = max(high[-20:]) if len(high) >= 20 else None
    volume_avg_5 = _avg(volume[-5:])
    volume_avg_20 = _avg(volume[-20:])
    volume_ratio_20 = _safe_div(latest_volume, volume_avg_20)

    ma = {
        "ma5": _last(indicators.get("ma5")),
        "ma10": _last(indicators.get("ma10")),
        "ma20": _last(indicators.get("ma20")),
        "ma60": _last(indicators.get("ma60")),
    }
    macd = indicators.get("macd") or {}
    macd_bar = _number_list(macd.get("bar"))
    kdj = indicators.get("kdj") or {}

    return {
        "stock": {
            "name": stock.get("name") or stock.get("code") or "",
            "code": stock.get("display_code") or stock.get("symbol") or stock.get("code") or "",
            "price": _round(stock.get("price")),
            "change_percent": _round(stock.get("change")),
            "amount": _round(stock.get("amount")),
            "volume": _round(stock.get("volume")),
        },
        "trend_summary": {
            "system_score": analysis.get("score"),
            "system_advice": analysis.get("advice"),
            "short_trend": analysis.get("short_trend"),
            "medium_trend": analysis.get("medium_trend"),
            "system_reason": analysis.get("reason"),
            "skill_trend": _infer_trend(latest_close, ma),
        },
        "price_action": {
            "latest_close": _round(latest_close),
            "latest_high": _round(latest_high),
            "latest_low": _round(latest_low),
            "return_5d_percent": _round(_period_return(close, 5)),
            "return_20d_percent": _round(_period_return(close, 20)),
            "return_60d_percent": _round(_period_return(close, 60)),
            "support_20d": _round(support_20),
            "resistance_20d": _round(resistance_20),
        },
        "technical_indicators": {
            "moving_averages": {key: _round(value) for key, value in ma.items()},
            "macd": {
                "dif": _round(_last(macd.get("dif"))),
                "dea": _round(_last(macd.get("dea"))),
                "bar": _round(_last(macd_bar)),
                "bar_change": _round(_bar_change(macd_bar)),
            },
            "kdj": {
                "k": _round(_last(kdj.get("k"))),
                "d": _round(_last(kdj.get("d"))),
                "j": _round(_last(kdj.get("j"))),
            },
            "rsi": {
                "rsi6": _round(_last(indicators.get("rsi6"))),
                "rsi12": _round(_last(indicators.get("rsi12"))),
                "rsi24": _round(_last(indicators.get("rsi24"))),
            },
            "boll": {
                "upper": _round(_last(indicators.get("boll_upper"))),
                "mid": _round(_last(indicators.get("boll_mid"))),
                "lower": _round(_last(indicators.get("boll_lower"))),
            },
        },
        "volume": {
            "latest": _round(latest_volume),
            "avg_5d": _round(volume_avg_5),
            "avg_20d": _round(volume_avg_20),
            "latest_vs_20d_avg": _round(volume_ratio_20),
        },
        "constraints": [
            "仅有行情和技术指标，没有财报、公告、新闻和板块实时排名。",
            "相关推荐只能作为观察方向，不能当作确定买卖建议。",
        ],
    }


def _number_list(values) -> list[float]:
    result = []
    for value in values or []:
        try:
            result.append(float(value))
        except Exception:
            continue
    return result


def _last(values) -> Optional[float]:
    items = _number_list(values) if not isinstance(values, (int, float)) else [float(values)]
    return items[-1] if items else None


def _avg(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _safe_div(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None or not right:
        return None
    return left / right


def _period_return(close: list[float], period: int) -> Optional[float]:
    if len(close) <= period or not close[-period - 1]:
        return None
    return (close[-1] - close[-period - 1]) / close[-period - 1] * 100


def _bar_change(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    return values[-1] - values[-2]


def _infer_trend(latest_close: Optional[float], ma: dict) -> str:
    if latest_close is None:
        return "数据不足"
    ma5 = ma.get("ma5")
    ma10 = ma.get("ma10")
    ma20 = ma.get("ma20")
    ma60 = ma.get("ma60")
    if all(value is not None for value in (ma5, ma10, ma20)) and latest_close > ma5 > ma10 > ma20:
        return "短线多头排列"
    if all(value is not None for value in (ma5, ma10, ma20)) and latest_close < ma5 < ma10 < ma20:
        return "短线空头排列"
    if ma60 is not None and latest_close > ma60:
        return "中期仍在 MA60 上方"
    if ma60 is not None and latest_close < ma60:
        return "中期在 MA60 下方"
    return "震荡"


def _round(value, digits: int = 2):
    try:
        return round(float(value), digits)
    except Exception:
        return None
