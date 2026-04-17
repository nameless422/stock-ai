from __future__ import annotations

import os
from time import sleep

import httpx

from app.config import settings
from app.core.strategy_engine import (
    build_strategy_context,
    get_strategy_contract,
    run_strategy_code,
)
from app.repositories.strategy_repository import StrategyRepository


strategy_repository = StrategyRepository(settings.db_path)


def get_target_options() -> dict:
    return {
        "strategies": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description", ""),
                "enabled": item.get("enabled", 1),
                "create_mode": item.get("create_mode", "direct"),
            }
            for item in strategy_repository.list_strategies()
        ],
        "groups": [
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description", ""),
                "match_mode": item.get("match_mode", "AND"),
                "strategy_ids": item.get("strategy_ids", []),
                "strategy_names": [strategy["name"] for strategy in item.get("strategies", [])],
            }
            for item in strategy_repository.list_strategy_groups()
        ],
    }


def resolve_screening_target(target_type: str | None = None, target_id: int | None = None) -> dict | None:
    target_type = target_type or "strategy"
    if target_type == "group":
        group = strategy_repository.get_strategy_group(int(target_id)) if target_id else None
        if not group:
            groups = strategy_repository.list_strategy_groups()
            group = groups[0] if groups else None
        if not group:
            return None
        strategies = [strategy_repository.get_strategy(strategy_id) for strategy_id in group.get("strategy_ids", [])]
        strategies = [item for item in strategies if item and item.get("enabled", 1)]
        if not strategies:
            return None
        return {
            "target_type": "group",
            "target_id": group["id"],
            "target_name": group["name"],
            "target_logic": group.get("match_mode", "AND").upper(),
            "strategies": strategies,
        }

    strategy = strategy_repository.get_strategy(int(target_id)) if target_id else None
    if strategy and not strategy.get("enabled", 1):
        strategy = None
    if not strategy:
        strategy_list = strategy_repository.list_strategies(enabled_only=True)
        strategy = strategy_list[0] if strategy_list else None
    if not strategy:
        return None
    return {
        "target_type": "strategy",
        "target_id": strategy["id"],
        "target_name": strategy["name"],
        "target_logic": "SINGLE",
        "strategies": [strategy],
    }


def build_strategy_generation_context(prompt_text: str) -> dict:
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    llm_api_key = os.getenv("LLM_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    api_key = minimax_api_key or llm_api_key or openai_api_key
    if not api_key:
        raise ValueError("未配置 MINIMAX_API_KEY、LLM_API_KEY 或 OPENAI_API_KEY")
    if minimax_api_key and api_key == minimax_api_key:
        base_url = (os.getenv("MINIMAX_API_BASE") or os.getenv("LLM_API_BASE") or "https://api.minimax.io/v1").rstrip("/")
        model = os.getenv("MINIMAX_MODEL") or os.getenv("LLM_MODEL") or "MiniMax-M2.5"
    else:
        base_url = (
            os.getenv("LLM_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    contract = get_strategy_contract()
    allowed_fields = [
        "context['stock']['code']",
        "context['stock']['name']",
        "context['stock']['symbol']",
        "context['data']['daily']['dates']",
        "context['data']['daily']['open']",
        "context['data']['daily']['close']",
        "context['data']['daily']['high']",
        "context['data']['daily']['low']",
        "context['data']['daily']['volume']",
        "context['data']['weekly']['dates']",
        "context['data']['weekly']['open']",
        "context['data']['weekly']['close']",
        "context['data']['weekly']['high']",
        "context['data']['weekly']['low']",
        "context['data']['weekly']['volume']",
        "context['snapshots']['daily']['enough_data']",
        "context['snapshots']['daily']['rows']",
        "context['snapshots']['daily']['latest_open']",
        "context['snapshots']['daily']['latest_close']",
        "context['snapshots']['daily']['latest_high']",
        "context['snapshots']['daily']['latest_low']",
        "context['snapshots']['daily']['current_volume']",
        "context['snapshots']['daily']['max_volume_3m']",
        "context['snapshots']['daily']['latest_dif']",
        "context['snapshots']['daily']['latest_dea']",
        "context['snapshots']['daily']['latest_macd_bar']",
        "context['snapshots']['weekly']['enough_data']",
        "context['snapshots']['weekly']['rows']",
        "context['snapshots']['weekly']['latest_open']",
        "context['snapshots']['weekly']['latest_close']",
        "context['snapshots']['weekly']['consecutive_red']",
        "context['snapshots']['weekly']['recent_red_bars']",
        "context['indicators']['daily']['ma5']",
        "context['indicators']['daily']['ma10']",
        "context['indicators']['daily']['ma20']",
        "context['indicators']['daily']['ma60']",
        "context['indicators']['daily']['macd']['dif']",
        "context['indicators']['daily']['macd']['dea']",
        "context['indicators']['daily']['macd']['bar']",
        "context['indicators']['weekly']['ma5']",
        "context['indicators']['weekly']['ma10']",
        "context['indicators']['weekly']['ma20']",
        "context['indicators']['weekly']['macd']['dif']",
        "context['indicators']['weekly']['macd']['dea']",
        "context['indicators']['weekly']['macd']['bar']",
    ]
    forbidden_fields = [
        "context['snapshots']['daily']['prev_close']",
        "context['snapshots']['daily']['change']",
        "context.symbol",
        "context.get_close(...)",
        "context.close",
        "backtrader",
        "talib / ta-lib",
    ]
    allowed_fields_text = "\n".join(f"- {item}" for item in allowed_fields)
    forbidden_fields_text = "\n".join(f"- {item}" for item in forbidden_fields)
    system_prompt = (
        "你是资深量化工程师。请根据用户要求输出可直接执行的 Python 策略代码。"
        "只能返回代码，不要解释，不要 Markdown，不要输出 <think>。"
        "必须定义 run_strategy(context) 函数，且只能使用项目已有的 context 字典结构。"
        "不要使用 backtrader、talib、context.symbol、context.get_close 之类项目中不存在的 API。"
    )
    user_prompt = (
        f"需求：{prompt_text}\n"
        "必须遵守这些约束：\n"
        "1. 只定义 run_strategy(context)。\n"
        "2. 只能使用下面这些已存在字段，不能猜测或发明新字段：\n"
        f"{allowed_fields_text}\n"
        "3. 必须使用 context['a']['b'] 这种字典访问方式。\n"
        "4. 返回 dict，至少包含 pass(bool) 和 reason(str)，可选 score、metrics。\n"
        "5. 数据不足时直接返回 pass=False 和明确原因。\n"
        "6. 严禁使用下面这些不存在或不允许的字段/库：\n"
        f"{forbidden_fields_text}\n"
        "7. 参考模板结构如下：\n"
        f"{contract['template']}\n"
        "现在只返回符合这些约束的完整 Python 代码。"
    )
    return {
        "model": model,
        "base_url": base_url,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }


def generate_strategy_code(prompt_text: str) -> str:
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    llm_api_key = os.getenv("LLM_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    api_key = minimax_api_key or llm_api_key or openai_api_key
    if not api_key:
        raise ValueError("未配置 MINIMAX_API_KEY、LLM_API_KEY 或 OPENAI_API_KEY")

    def extract_code(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"策略生成返回异常，缺少 choices: {data}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ValueError(f"策略生成返回空内容: {data}")
        if "<think>" in content and "</think>" in content:
            content = content.split("</think>", 1)[1].strip()
        if content.startswith("```"):
            content = content.strip("`").strip()
            if content.startswith("python"):
                content = content[6:].lstrip()
        return content.strip()

    def call_llm(model_name: str, base_url: str, messages: list[dict], temperature: float) -> dict:
        payloads = [
            {"model": model_name, "messages": messages, "temperature": temperature},
            {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "只返回 Python 代码，定义 run_strategy(context)，不要解释。"},
                    {"role": "user", "content": messages[-1]["content"]},
                ],
                "temperature": 0.1,
            },
        ]
        last_error = None
        with httpx.Client(timeout=90) as client:
            for index, payload in enumerate(payloads):
                try:
                    response = client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code == 529 and index < len(payloads) - 1:
                        sleep(1)
                        continue
                    raise
        raise last_error or ValueError("策略生成失败")

    llm_context = build_strategy_generation_context(prompt_text)
    test_context = build_strategy_context({"code": "000001", "name": "平安银行", "symbol": "sz000001"}, [], [])
    contract = get_strategy_contract()

    generated = extract_code(
        call_llm(
            llm_context["model"],
            llm_context["base_url"],
            [
                {"role": "system", "content": llm_context["system_prompt"]},
                {"role": "user", "content": llm_context["user_prompt"]},
            ],
            0.2,
        )
    )

    validation = run_strategy_code(generated, test_context)
    if validation.get("error"):
        repair_prompt = (
            "下面这段策略代码不符合项目约定，请修复后只返回完整 Python 代码。\n"
            f"错误：{validation.get('reason', '未知错误')}\n"
            "项目约定：只能访问 context['stock']、context['snapshots']、context['indicators']；"
            "必须定义 run_strategy(context)；返回 dict。\n"
            f"参考模板：\n{contract['template']}\n"
            f"待修复代码：\n{generated}"
        )
        generated = extract_code(
            call_llm(
                llm_context["model"],
                llm_context["base_url"],
                [
                    {"role": "system", "content": llm_context["system_prompt"]},
                    {"role": "user", "content": repair_prompt},
                ],
                0.1,
            )
        )
        validation = run_strategy_code(generated, test_context)
        if validation.get("error"):
            raise ValueError(validation.get("reason", "策略代码校验失败"))
    return generated
