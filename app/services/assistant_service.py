from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional

import httpx

from db import compat as db
from app.config import settings
from app.core.llm_provider import resolve_llm_provider
from app.core.stock_consulting_skill import (
    build_market_recommendation_messages,
    build_stock_consulting_messages,
    build_theme_stock_recommendation_messages,
    format_theme_stock_recommendation_fallback,
    match_theme_stock_recommendation,
)
from app.core.strategy_reviewer import review_strategy_code
from app.repositories.assistant_repository import AssistantRepository
from app.services.market_service import get_quote_bundle_async, get_stock_info_async, search_stock_async
from app.services.screener_service import enqueue_screening_task
from app.services.strategy_service import generate_strategy_code, strategy_repository


assistant_repository = AssistantRepository(settings.db_path)

STRATEGY_CONDITION_KEYWORDS = (
    "k线",
    "日线",
    "周线",
    "月线",
    "均线",
    "年线",
    "半年线",
    "ma",
    "macd",
    "dif",
    "dea",
    "金叉",
    "死叉",
    "站上",
    "跌破",
    "突破",
    "支撑",
    "压力",
    "趋势线",
    "成交量",
    "成交额",
    "成交金额",
    "交易额",
    "交易额度",
    "量能",
    "放量",
    "缩量",
    "换手",
    "换手率",
    "量比",
    "资金流",
)

GENERAL_ANSWERS = {
    "macd": (
        "MACD 常用来观察趋势和动能。DIF 上穿 DEA 通常叫金叉，代表短期动能转强；"
        "DIF 下穿 DEA 通常叫死叉，代表动能转弱。实际判断还要结合成交量、均线位置和大盘环境。"
    ),
    "kdj": (
        "KDJ 偏向短线摆动指标。K、D 低位向上时代表短线修复概率提高，"
        "高位钝化时不能单独作为卖出依据，需要结合趋势和量能。"
    ),
    "rsi": (
        "RSI 用来观察涨跌强弱。一般低位代表短期偏弱或超卖，高位代表短期偏强或超买，"
        "但强趋势里 RSI 可能长期停留在高位或低位。"
    ),
}

ASSISTANT_INTENTS = {"concept", "related_stocks", "screening", "market", "other"}


def classify_assistant_intent(text: str) -> str:
    content = (text or "").strip().lower()
    if not content:
        return "other"
    if _looks_like_screening_request(content):
        return "screening"
    if _looks_like_related_stocks_question(content):
        return "related_stocks"
    if _looks_like_market_question(content):
        return "market"
    if _looks_like_concept_question(content):
        return "concept"
    return "other"


async def process_assistant_message(
    message: str,
    session_id: str,
    client: httpx.AsyncClient,
    model: Optional[str] = None,
) -> dict:
    user_text = (message or "").strip()
    safe_session_id = (session_id or "default").strip()[:64] or "default"
    model_choice = _normalize_model_choice(model)
    session_context = _load_session_context(safe_session_id)
    intent_result = await _classify_assistant_intent_with_ai(user_text, session_context, client, model_choice)
    intent = intent_result["intent"]
    if intent == "screening":
        return await _handle_screening_request(user_text, safe_session_id, intent_result)
    return await _handle_stock_question(user_text, safe_session_id, client, session_context, intent_result, model_choice)


def get_assistant_model_options() -> dict:
    try:
        llm_config = resolve_llm_provider()
    except Exception as exc:
        return {"default_model": "", "models": [], "error": str(exc)}
    models = []
    seen = set()
    for value in [llm_config.model, *_configured_chat_models()]:
        model = _clean_model_name(value)
        if not model or model in seen:
            continue
        models.append({"value": model, "label": model})
        seen.add(model)
    return {
        "provider": llm_config.provider,
        "base_url": llm_config.base_url,
        "default_model": llm_config.model,
        "models": models,
    }


def list_assistant_history(session_id: Optional[str], limit: int = 50) -> list[dict]:
    safe_session_id = (session_id or "").strip() or None
    if safe_session_id:
        return assistant_repository.list_messages(safe_session_id, limit=limit, ascending=True)
    return assistant_repository.list_sessions(limit=limit)


def clear_assistant_history(session_id: Optional[str]) -> int:
    return assistant_repository.clear_messages((session_id or "").strip() or None)


async def _handle_screening_request(user_text: str, session_id: str, intent_result: Optional[dict] = None) -> dict:
    try:
        result = await asyncio.to_thread(_create_strategy_and_enqueue, user_text)
        strategy = result["strategy"]
        task = result["task"]
        redirect_url = _build_screener_url(strategy, task)
        reply = (
            f"已根据你的描述创建策略「{strategy['name']}」，并把选股任务加入队列。"
            "页面将跳转到任务面板查看进度和结果。"
        )
        record = assistant_repository.create_message(
            session_id=session_id,
            intent="screening",
            user_text=user_text,
            assistant_text=reply,
            action_type="create_strategy_and_run",
            strategy_id=int(strategy["id"]),
            strategy_name=strategy["name"],
            task_id=int(task["id"]),
            run_token=task.get("run_token", ""),
            metadata={
                "redirect_url": redirect_url,
                "target_type": "strategy",
                "target_id": strategy["id"],
                "target_name": strategy["name"],
                "intent_reason": (intent_result or {}).get("reason", ""),
                "intent_source": (intent_result or {}).get("source", ""),
            },
        )
        return {
            "ok": True,
            "intent": "screening",
            "reply": reply,
            "strategy": strategy,
            "task": task,
            "redirect_url": redirect_url,
            "history_item": record,
        }
    except Exception as exc:
        reply = f"自动创建并运行选股策略失败：{str(exc)}"
        record = assistant_repository.create_message(
            session_id=session_id,
            intent="screening",
            user_text=user_text,
            assistant_text=reply,
            action_type="create_strategy_failed",
            metadata={
                "error": str(exc),
                "intent_reason": (intent_result or {}).get("reason", ""),
                "intent_source": (intent_result or {}).get("source", ""),
            },
        )
        return {
            "ok": False,
            "intent": "screening",
            "reply": reply,
            "history_item": record,
        }


def _create_strategy_and_enqueue(user_text: str) -> dict:
    code = generate_strategy_code(user_text)
    review = review_strategy_code(code)
    if not review.get("ok"):
        raise ValueError("策略代码审查或样例试跑未通过")

    strategy = None
    last_error = None
    for index in range(6):
        name = _build_strategy_name(user_text, index)
        try:
            strategy = strategy_repository.create_strategy(
                name=name,
                description=f"由股票助手根据自然语言自动生成：{user_text}",
                code=code,
                create_mode="ai",
                enabled=1,
            )
            break
        except db.IntegrityError as exc:
            last_error = exc
    if not strategy:
        raise ValueError(f"策略名称重复，保存失败：{last_error}")

    task = enqueue_screening_task(target_type="strategy", target_id=int(strategy["id"]), source="assistant")
    return {"strategy": strategy, "task": task}


def _build_strategy_name(user_text: str, index: int = 0) -> str:
    now_text = datetime.now(settings.market_tz).strftime("%m%d%H%M%S")
    compact = re.sub(r"\s+", "", user_text)
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)
    summary = compact[:12] or "自然语言"
    suffix = f"-{index + 1}" if index else ""
    return f"助手策略-{now_text}-{summary}{suffix}"


def _build_screener_url(strategy: dict, task: dict) -> str:
    target_name = str(strategy.get("name") or "")
    params = {
        "target_type": "strategy",
        "target_id": str(strategy["id"]),
        "target_name": target_name,
        "guided": "1",
    }
    task_id = task.get("id")
    run_token = task.get("run_token")
    if task_id:
        params["task_id"] = str(task_id)
    if run_token:
        params["run_token"] = str(run_token)
    from urllib.parse import urlencode

    return f"/screener?{urlencode(params)}"


async def _handle_stock_question(
    user_text: str,
    session_id: str,
    client: httpx.AsyncClient,
    session_context: Optional[list[dict]] = None,
    intent_result: Optional[dict] = None,
    model_choice: str = "",
) -> dict:
    session_context = session_context or []
    assistant_intent = (intent_result or {}).get("intent") or "other"
    stock_code, stock_hint = await _resolve_stock_from_text(user_text, client)
    if not stock_code:
        stock_code, stock_hint = _resolve_stock_from_context(user_text, session_context)
    if not stock_code:
        fallback = _answer_general_stock_question(user_text)
        theme_payload = match_theme_stock_recommendation(user_text) if assistant_intent == "related_stocks" else None
        if assistant_intent == "related_stocks" and theme_payload:
            theme_payload = await _build_theme_payload_with_quotes(theme_payload, client)
            fallback = format_theme_stock_recommendation_fallback(theme_payload)
            answer = await _generate_theme_stock_recommendation_answer(
                user_text,
                session_context,
                client,
                fallback,
                theme_payload,
                model_choice,
            )
            action_type = "answer_theme_stock_question"
            skill_name = "theme_stock_recommendation"
        elif assistant_intent == "market":
            answer = await _generate_market_recommendation_answer(user_text, session_context, client, fallback, model_choice)
            action_type = "answer_market_question"
            skill_name = "market_recommendation"
        else:
            answer = await _generate_stock_chat_answer(
                user_text=user_text,
                session_context=session_context,
                client=client,
                fallback_answer=fallback,
                model_choice=model_choice,
            )
            action_type = "answer_stock_question"
            skill_name = "general_chat"
        record = assistant_repository.create_message(
            session_id=session_id,
            intent=assistant_intent,
            user_text=user_text,
            assistant_text=answer,
            action_type=action_type,
            metadata={
                "intent_reason": (intent_result or {}).get("reason", ""),
                "intent_source": (intent_result or {}).get("source", ""),
                "assistant_skill": skill_name,
                "model": model_choice,
                "theme": (theme_payload or {}).get("theme") if theme_payload else "",
                "candidate_stocks": [
                    {"code": item.get("code"), "name": item.get("name")}
                    for item in ((theme_payload or {}).get("stocks") or [])
                ],
            },
        )
        return {"ok": True, "intent": assistant_intent, "reply": answer, "history_item": record}

    bundle = await get_quote_bundle_async(stock_code, "daily", "qfq", client)
    if bundle.get("error"):
        answer = f"没有拿到 {stock_code} 的完整行情数据：{bundle.get('error')}"
        record = assistant_repository.create_message(
            session_id=session_id,
            intent=assistant_intent,
            user_text=user_text,
            assistant_text=answer,
            action_type="answer_stock_question",
            stock_code=stock_code,
            stock_name=(stock_hint or {}).get("name", ""),
            metadata={
                "error": bundle.get("error"),
                "intent_reason": (intent_result or {}).get("reason", ""),
                "intent_source": (intent_result or {}).get("source", ""),
                "assistant_skill": "stock_consulting",
                "model": model_choice,
            },
        )
        return {"ok": False, "intent": assistant_intent, "reply": answer, "history_item": record}

    fallback = _format_stock_answer(bundle)
    answer = await _generate_stock_chat_answer(
        user_text=user_text,
        session_context=session_context,
        client=client,
        fallback_answer=fallback,
        bundle=bundle,
        model_choice=model_choice,
    )
    stock = bundle.get("stock") or {}
    record = assistant_repository.create_message(
        session_id=session_id,
        intent=assistant_intent,
        user_text=user_text,
        assistant_text=answer,
        action_type="answer_stock_question",
        stock_code=stock.get("code") or stock_code,
        stock_name=stock.get("name") or (stock_hint or {}).get("name", ""),
        metadata={
            "symbol": stock.get("symbol", ""),
            "display_code": stock.get("display_code", ""),
            "analysis": bundle.get("analysis") or {},
            "intent_reason": (intent_result or {}).get("reason", ""),
            "intent_source": (intent_result or {}).get("source", ""),
            "assistant_skill": "stock_consulting",
            "model": model_choice,
        },
    )
    return {
        "ok": True,
        "intent": assistant_intent,
        "reply": answer,
        "stock": stock,
        "history_item": record,
    }


def _looks_like_screening_request(content: str) -> bool:
    content = re.sub(r"\s+", "", content or "")
    has_condition = any(keyword in content for keyword in STRATEGY_CONDITION_KEYWORDS)
    has_select_stock = bool(re.search(r"(找|选|筛|挑).{0,32}(股票|a股|票)", content))
    has_global = bool(re.search(r"(全市场|全部a股|所有a股|所有股票|a股里|市场里|全量|全局|批量|扫描)", content))
    has_run_strategy = bool(
        re.search(r"(跑|运行|执行).{0,12}(选股|策略|扫描|筛选)", content)
        or re.search(r"(用|按|按照|根据).{0,8}策略.{0,16}(跑|运行|执行|筛|选|扫描)", content)
    )
    has_create_strategy_run = bool(
        re.search(r"(新建|创建|生成).{0,8}策略.{0,20}(跑|运行|执行|筛|选|扫描|全市场|批量)", content)
    )
    if has_condition and (has_select_stock or has_global or has_run_strategy or has_create_strategy_run):
        return True
    if has_global and (has_run_strategy or has_create_strategy_run):
        return True
    return False


def _looks_like_related_stocks_question(text: str) -> bool:
    content = re.sub(r"\s+", "", (text or "").lower())
    if re.search(r"(相关(股票|标的|票)|概念股|龙头股|产业链.{0,12}(股票|标的|票|龙头))", content):
        return True
    if re.search(r"(找|选|筛|挑|推荐|有哪些).{0,18}(相关|概念|板块|行业|产业链|主题).{0,18}(股票|标的|票|龙头)", content):
        return True
    if match_theme_stock_recommendation(text) and re.search(r"(股票|标的|票|龙头|相关|概念股|有哪些|找一些|推荐)", content):
        return True
    return False


def _looks_like_market_question(text: str) -> bool:
    content = re.sub(r"\s+", "", (text or "").lower())
    return bool(
        re.search(r"(大盘|市场|行情|指数|上证|深成指|创业板|板块|行业|题材|主线|风格|北向|量能|风险偏好|宏观)", content)
    )


def _looks_like_concept_question(text: str) -> bool:
    content = re.sub(r"\s+", "", (text or "").lower())
    return bool(
        re.search(r"(是什么|什么意思|啥意思|解释|概念|原理|怎么理解|区别|适合.*吗|怎么看待)", content)
    )


def _load_session_context(session_id: str, limit: int = 8) -> list[dict]:
    try:
        rows = assistant_repository.list_messages(session_id, limit=limit, ascending=False)
    except Exception:
        return []
    return list(reversed(rows))


async def _classify_assistant_intent_with_ai(
    user_text: str,
    session_context: list[dict],
    client: httpx.AsyncClient,
    model_choice: str = "",
) -> dict:
    rule_intent = classify_assistant_intent(user_text)
    fallback = {
        "intent": rule_intent,
        "source": "rule",
        "confidence": 0.55 if rule_intent != "other" else 0.45,
        "reason": "本地规则判断",
    }
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是股票助手的意图分流器，只返回 JSON，不要 Markdown。"
                    "intent 只能是 concept、related_stocks、screening、market、other。"
                    "concept 表示用户想了解概念、指标、术语、策略原理或某主题是什么。"
                    "related_stocks 表示用户想找某行业/题材/产业链相关股票或概念股。"
                    "screening 表示用户明确要新建/使用策略并全市场/批量运行筛选。"
                    "market 表示用户想了解大盘、指数、板块、行业、市场环境或主线。"
                    "other 表示单只股票分析、上下文追问、模糊咨询或不属于上面类别的问题。"
                    "你必须先按用户真实意图分类，再由系统按 intent 执行对应动作。"
                    "只提到「策略」「选股」「MACD」但像是在咨询时，不要判为 screening。"
                    "只有当用户意图是把条件策略化并全局执行，而不是聊天咨询时，才判为 screening。"
                    "返回格式：{\"intent\":\"concept\",\"confidence\":0.0,\"reason\":\"一句话原因\"}。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前用户消息：{user_text}\n"
                    f"最近会话上下文：\n{_format_session_context(session_context)}\n"
                    f"本地规则兜底提示：{rule_intent}\n"
                    "请严格按规则输出 JSON。"
                ),
            },
        ]
        content = await _call_llm_chat_completion(client, messages, temperature=0.0, max_tokens=220, model_choice=model_choice)
        data = _extract_json_object(content)
        intent = data.get("intent")
        if intent not in ASSISTANT_INTENTS:
            return fallback
        confidence = _safe_float(data.get("confidence"), 0.0)
        if confidence < 0.5:
            return {
                "intent": "other",
                "source": "ai",
                "confidence": confidence,
                "reason": str(data.get("reason") or "AI 判断信心不足，按其他咨询处理"),
            }
        if intent == "screening" and rule_intent != "screening" and not _has_screening_execution_signal(user_text):
            return {
                "intent": rule_intent if rule_intent != "other" else "other",
                "source": "ai",
                "confidence": confidence,
                "reason": str(data.get("reason") or "AI 判断为筛选，但用户没有明确全局执行信号，按咨询处理"),
            }
        return {
            "intent": intent,
            "source": "ai",
            "confidence": confidence,
            "reason": str(data.get("reason") or ""),
        }
    except Exception as exc:
        fallback["reason"] = f"AI 意图判断不可用，使用本地规则：{exc}"
        return fallback


async def _generate_stock_chat_answer(
    *,
    user_text: str,
    session_context: list[dict],
    client: httpx.AsyncClient,
    fallback_answer: str,
    bundle: Optional[dict] = None,
    model_choice: str = "",
) -> str:
    try:
        if bundle:
            messages = build_stock_consulting_messages(user_text, _format_session_context(session_context), bundle)
        else:
            messages = _build_general_stock_chat_messages(user_text, session_context)
        answer = (await _call_llm_chat_completion(client, messages, temperature=0.35, max_tokens=1000, model_choice=model_choice)).strip()
        if not answer:
            return fallback_answer
        if _looks_like_investment_advice(user_text) and "不构成投资建议" not in answer:
            answer = answer.rstrip() + "\n以上仅为技术面和公开行情信息的自动归纳，不构成投资建议。"
        return answer
    except Exception:
        return fallback_answer


async def _generate_market_recommendation_answer(
    user_text: str,
    session_context: list[dict],
    client: httpx.AsyncClient,
    fallback_answer: str,
    model_choice: str = "",
) -> str:
    try:
        messages = build_market_recommendation_messages(user_text, _format_session_context(session_context))
        answer = (await _call_llm_chat_completion(client, messages, temperature=0.35, max_tokens=900, model_choice=model_choice)).strip()
        return answer or fallback_answer
    except Exception:
        return fallback_answer


async def _generate_theme_stock_recommendation_answer(
    user_text: str,
    session_context: list[dict],
    client: httpx.AsyncClient,
    fallback_answer: str,
    theme_payload: dict,
    model_choice: str = "",
) -> str:
    try:
        messages = build_theme_stock_recommendation_messages(user_text, _format_session_context(session_context), theme_payload)
        answer = (await _call_llm_chat_completion(client, messages, temperature=0.25, max_tokens=1100, model_choice=model_choice)).strip()
        if not answer:
            return fallback_answer
        if "不构成投资建议" not in answer:
            answer = answer.rstrip() + "\n以上只是主题相关观察标的，不构成投资建议。"
        return answer
    except Exception:
        return fallback_answer


def _build_general_stock_chat_messages(user_text: str, session_context: list[dict]) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "你是面向中文用户的 A 股股票助手。"
                "你可以结合最近会话上下文回答追问，但不要声称已经运行选股策略或创建任务。"
                "如果用户只是模糊咨询，直接给出咨询式回答；如果需要具体股票但上下文也没有，温和提示补充股票代码或名称。"
                "回答要简洁，优先解释判断依据、风险和下一步观察点。"
                "不要给确定收益承诺，涉及买卖时必须说明不构成投资建议。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"用户问题：{user_text}\n\n"
                f"最近会话上下文：\n{_format_session_context(session_context)}\n\n"
                "本轮没有定位到具体股票行情。请直接给出给用户看的中文回复。"
            ),
        },
    ]


async def _call_llm_chat_completion(
    client: httpx.AsyncClient,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    model_choice: str = "",
) -> str:
    llm_config = resolve_llm_provider()
    selected_model = _select_chat_model(llm_config.model, model_choice)
    payload = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if llm_config.provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}

    async def post(data: dict) -> httpx.Response:
        return await client.post(
            f"{llm_config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm_config.api_key}",
                "Content-Type": "application/json",
            },
            json=data,
            timeout=45,
        )

    response = await post(payload)
    if response.status_code == 400 and payload.get("thinking"):
        retry_payload = dict(payload)
        retry_payload.pop("thinking", None)
        response = await post(retry_payload)
    response.raise_for_status()
    return _extract_message_content(response.json())


def _extract_message_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content).strip()


def _extract_json_object(content: str) -> dict:
    text = (content or "").strip()
    if "<think>" in text and "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _format_session_context(rows: list[dict], limit: int = 6) -> str:
    if not rows:
        return "无"
    lines = []
    for row in rows[-limit:]:
        extras = []
        if row.get("stock_code"):
            stock_label = row.get("stock_name") or row.get("stock_code")
            extras.append(f"股票={stock_label}({row.get('stock_code')})")
        if row.get("strategy_name"):
            extras.append(f"策略={row.get('strategy_name')}")
        if row.get("run_token"):
            extras.append(f"任务={row.get('run_token')}")
        if row.get("action_type"):
            extras.append(f"动作={row.get('action_type')}")
        extra_text = f"；结果信息：{'，'.join(extras)}" if extras else ""
        lines.append(
            "用户："
            + _clip_text(row.get("user_text") or "", 160)
            + "\n助手："
            + _clip_text(row.get("assistant_text") or "", 260)
            + extra_text
        )
    return "\n---\n".join(lines)


def _format_quote_context(bundle: Optional[dict]) -> str:
    if not bundle:
        return "无"
    stock = bundle.get("stock") or {}
    analysis = bundle.get("analysis") or {}
    indicators = bundle.get("indicators") or {}
    macd = indicators.get("macd") or {}
    payload = {
        "stock": {
            "name": stock.get("name"),
            "code": stock.get("display_code") or stock.get("code"),
            "price": stock.get("price"),
            "change": stock.get("change"),
        },
        "analysis": {
            "score": analysis.get("score"),
            "advice": analysis.get("advice"),
            "short_trend": analysis.get("short_trend"),
            "medium_trend": analysis.get("medium_trend"),
            "reason": analysis.get("reason"),
        },
        "macd_latest": {
            "dif": _last_number(macd.get("dif")),
            "dea": _last_number(macd.get("dea")),
            "bar": _last_number(macd.get("bar")),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


async def _build_theme_payload_with_quotes(theme_payload: dict, client: httpx.AsyncClient) -> dict:
    payload = json.loads(json.dumps(theme_payload, ensure_ascii=False))
    stocks = payload.get("stocks") or []

    async def fetch_quote(item: dict) -> dict:
        stock_code = str(item.get("code") or "").strip()
        if not stock_code:
            return {}
        info = await get_stock_info_async(stock_code, client)
        if info.get("error"):
            return {"error": info.get("error")}
        return {
            "price": _format_number(info.get("price")),
            "change": _format_signed(info.get("change"), suffix="%"),
            "amount": _format_number((float(info.get("amount") or 0) / 100000000) if info.get("amount") is not None else None),
        }

    quotes = await asyncio.gather(*(fetch_quote(item) for item in stocks[:10]), return_exceptions=True)
    for item, quote in zip(stocks[:10], quotes):
        item["quote"] = {} if isinstance(quote, Exception) else quote
    return payload


def _configured_chat_models() -> list[str]:
    raw = os.getenv("ASSISTANT_CHAT_MODELS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _clean_model_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._:/-]", "", str(value or "").strip())[:80]


def _normalize_model_choice(value: Optional[str]) -> str:
    requested = _clean_model_name(value or "")
    if not requested:
        return ""
    options = get_assistant_model_options()
    allowed = {item["value"] for item in options.get("models", [])}
    return requested if requested in allowed else ""


def _select_chat_model(default_model: str, model_choice: str) -> str:
    requested = _normalize_model_choice(model_choice)
    return requested or default_model


def _resolve_stock_from_context(text: str, rows: list[dict]) -> tuple[Optional[str], Optional[dict]]:
    if not _should_use_context_stock(text):
        return None, None
    for row in reversed(rows):
        stock_code = (row.get("stock_code") or "").strip()
        if stock_code:
            return stock_code, {"name": row.get("stock_name") or "", "code": stock_code}
    return None, None


def _should_use_context_stock(text: str) -> bool:
    content = re.sub(r"\s+", "", text or "")
    if not content:
        return False
    return bool(
        re.search(
            r"(它|这只|这票|这个|该股|刚才|上面|前面|上一只|那只|继续|还能|还可以|能买吗|能不能买|怎么看|走势|后面|明天|持有|卖|买)",
            content,
        )
    )


def _has_screening_execution_signal(text: str) -> bool:
    content = re.sub(r"\s+", "", (text or "").lower())
    return bool(
        re.search(r"(全市场|全部a股|所有a股|所有股票|全量|全局|批量|扫描|筛选|找出|选出|挑出|跑|运行|执行)", content)
        or re.search(r"(新建|创建|生成).{0,8}策略", content)
        or re.search(r"(用|按|按照|根据).{0,8}策略", content)
    )


def _looks_like_investment_advice(text: str) -> bool:
    return bool(re.search(r"(买|卖|持有|加仓|减仓|止盈|止损|目标价|能买吗|能不能买|建议)", text or ""))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clip_text(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


async def _resolve_stock_from_text(text: str, client: httpx.AsyncClient) -> tuple[Optional[str], Optional[dict]]:
    code_match = re.search(r"(?<!\d)(?:sh|sz|bj)?(\d{6})(?!\d)", text, flags=re.IGNORECASE)
    if code_match:
        return code_match.group(1), None

    keyword = _extract_stock_keyword(text)
    if not keyword:
        return None, None
    result = await search_stock_async(keyword, client)
    items = result.get("results") or []
    if not items:
        return None, None
    first = items[0]
    return first.get("code"), first


def _extract_stock_keyword(text: str) -> str:
    content = re.sub(r"\s+", "", text or "")
    patterns = [
        r"([A-Za-z0-9\u4e00-\u9fff]{2,14})(?:股票|这只票|这票)?(?:怎么样|如何|怎么看|走势|股价|价格|分析|能买吗|能不能买)",
        r"(?:看一下|查一下|分析一下|问一下|咨询一下)([A-Za-z0-9\u4e00-\u9fff]{2,14})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            keyword = match.group(1)
            keyword = re.sub(r"^(帮我|请|请问|想问|我想问)", "", keyword)
            if keyword:
                return keyword[:14]
    return ""


def _answer_general_stock_question(text: str) -> str:
    lower = (text or "").lower()
    for keyword, answer in GENERAL_ANSWERS.items():
        if keyword in lower:
            return answer
    return "我没有识别到具体股票。请带上 6 位股票代码或股票名称，例如「贵州茅台怎么样」或「分析 000001」。"


def _format_stock_answer(bundle: dict) -> str:
    stock = bundle.get("stock") or {}
    analysis = bundle.get("analysis") or {}
    indicators = bundle.get("indicators") or {}
    macd = (indicators.get("macd") or {})
    dif = _last_number(macd.get("dif"))
    dea = _last_number(macd.get("dea"))
    bar = _last_number(macd.get("bar"))
    name = stock.get("name") or stock.get("code") or "这只股票"
    code = stock.get("display_code") or stock.get("symbol") or stock.get("code") or ""
    price = _format_number(stock.get("price"))
    change = _format_signed(stock.get("change"), suffix="%")
    score = analysis.get("score", "-")
    advice = analysis.get("advice", "观望")
    short_trend = analysis.get("short_trend", "-")
    medium_trend = analysis.get("medium_trend", "-")
    reason = analysis.get("reason", "暂无技术面原因")
    return (
        f"{name}（{code}）当前价 {price} 元，涨跌幅 {change}。\n"
        f"技术面评分 {score}/10，结论偏向「{advice}」。短期趋势：{short_trend}；中期趋势：{medium_trend}。\n"
        f"MACD：DIF {_format_number(dif)}，DEA {_format_number(dea)}，柱值 {_format_number(bar)}。\n"
        f"原因：{reason}\n"
        "以上只按当前行情和技术指标自动归纳，不构成投资建议。"
    )


def _last_number(values) -> Optional[float]:
    if not values:
        return None
    try:
        return float(values[-1])
    except Exception:
        return None


def _format_number(value) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _format_signed(value, suffix: str = "") -> str:
    try:
        number = float(value)
    except Exception:
        return "-"
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}{suffix}"
