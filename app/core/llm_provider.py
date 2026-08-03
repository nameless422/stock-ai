from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMProviderConfig:
    api_key: str
    base_url: str
    model: str
    provider: str


def resolve_llm_provider() -> LLMProviderConfig:
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
    llm_api_key = os.getenv("LLM_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    api_key = deepseek_api_key or llm_api_key or openai_api_key
    if not api_key:
        raise ValueError("未配置 DEEPSEEK_API_KEY、LLM_API_KEY 或 OPENAI_API_KEY")

    if deepseek_api_key and api_key == deepseek_api_key:
        return LLMProviderConfig(
            api_key=api_key,
            base_url=(os.getenv("DEEPSEEK_API_BASE") or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL") or os.getenv("LLM_MODEL") or "deepseek-v4-flash",
            provider="deepseek",
        )

    return LLMProviderConfig(
        api_key=api_key,
        base_url=(
            os.getenv("LLM_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        ).rstrip("/"),
        model=os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        provider="openai",
    )
