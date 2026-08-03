import asyncio
import unittest
from unittest.mock import patch

from app.services import assistant_service
from app.services.assistant_service import (
    _classify_assistant_intent_with_ai,
    _extract_stock_keyword,
    _format_stock_answer,
    classify_assistant_intent,
    get_assistant_model_options,
)
from app.core.stock_consulting_skill import (
    format_theme_stock_recommendation_fallback,
    match_theme_stock_recommendation,
)


class AssistantServiceTest(unittest.TestCase):
    def test_classifies_screening_requests(self):
        self.assertEqual(classify_assistant_intent("帮我选出 MACD 金叉且站上 20 日线的股票"), "screening")
        self.assertEqual(classify_assistant_intent("创建一个放量突破策略并跑一下"), "screening")
        self.assertEqual(classify_assistant_intent("用策略全市场跑一下 5 日线上穿 20 日线的股票"), "screening")
        self.assertEqual(classify_assistant_intent("根据策略全量去跑一下"), "screening")
        self.assertEqual(classify_assistant_intent("筛选最近成交量明显放大的 A 股"), "screening")

    def test_classifies_consulting_intents(self):
        self.assertEqual(classify_assistant_intent("贵州茅台怎么样"), "other")
        self.assertEqual(classify_assistant_intent("MACD 是什么"), "concept")
        self.assertEqual(classify_assistant_intent("MACD 策略适合短线吗"), "concept")
        self.assertEqual(classify_assistant_intent("有哪些股票值得买"), "other")
        self.assertEqual(classify_assistant_intent("最近大盘和新能源板块怎么看"), "market")
        self.assertEqual(classify_assistant_intent("帮我找一些光模块相关的股票"), "related_stocks")

    def test_ai_intent_classifier_runs_before_execution(self):
        async def fake_completion(*args, **kwargs):
            return '{"intent":"related_stocks","confidence":0.92,"reason":"用户想找题材相关股票"}'

        with patch("app.services.assistant_service._call_llm_chat_completion", side_effect=fake_completion):
            result = asyncio.run(
                _classify_assistant_intent_with_ai(
                    "帮我找一些光模块相关的股票",
                    [],
                    client=None,
                    model_choice="deepseek-v4-pro",
                )
            )

        self.assertEqual(result["intent"], "related_stocks")
        self.assertEqual(result["source"], "ai")

    def test_ai_screening_intent_requires_execution_signal(self):
        async def fake_completion(*args, **kwargs):
            return '{"intent":"screening","confidence":0.95,"reason":"提到策略"}'

        with patch("app.services.assistant_service._call_llm_chat_completion", side_effect=fake_completion):
            result = asyncio.run(
                _classify_assistant_intent_with_ai(
                    "MACD 策略适合短线吗",
                    [],
                    client=None,
                    model_choice="deepseek-v4-pro",
                )
            )

        self.assertEqual(result["intent"], "concept")
        self.assertEqual(result["source"], "ai")

    def test_extracts_stock_keyword_from_question(self):
        self.assertEqual(_extract_stock_keyword("贵州茅台怎么样"), "贵州茅台")
        self.assertEqual(_extract_stock_keyword("帮我分析一下平安银行"), "平安银行")

    def test_formats_stock_answer(self):
        answer = _format_stock_answer(
            {
                "stock": {
                    "name": "平安银行",
                    "code": "000001",
                    "display_code": "000001",
                    "price": 10.5,
                    "change": 1.23,
                },
                "analysis": {
                    "score": 6.8,
                    "advice": "建议",
                    "short_trend": "上涨",
                    "medium_trend": "震荡",
                    "reason": "均线震荡，MACD金叉",
                },
                "indicators": {
                    "macd": {
                        "dif": [0.1],
                        "dea": [0.05],
                        "bar": [0.1],
                    },
                },
            }
        )

        self.assertIn("平安银行（000001）", answer)
        self.assertIn("技术面评分 6.8/10", answer)
        self.assertIn("不构成投资建议", answer)

    def test_gets_configured_assistant_model_options(self):
        env = {
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "default-model",
            "ASSISTANT_CHAT_MODELS": "default-model,fast-model,strong-model",
        }
        with patch.dict("os.environ", env, clear=True):
            options = get_assistant_model_options()

        self.assertEqual(options["default_model"], "default-model")
        self.assertEqual([item["value"] for item in options["models"]], ["default-model", "fast-model", "strong-model"])

    def test_matches_light_module_theme_recommendation(self):
        payload = match_theme_stock_recommendation("帮我找一些光模块相关的股票")

        self.assertIsNotNone(payload)
        answer = format_theme_stock_recommendation_fallback(payload)
        self.assertIn("中际旭创（300308）", answer)
        self.assertIn("新易盛（300502）", answer)
        self.assertIn("不构成投资建议", answer)

    def test_lists_sessions_when_no_session_id(self):
        class FakeRepository:
            def list_sessions(self, limit=50):
                return [{"session_id": "s1", "message_count": 2}]

            def list_messages(self, session_id=None, limit=50, ascending=False):
                raise AssertionError("不应按消息查询")

        original_repository = assistant_service.assistant_repository
        assistant_service.assistant_repository = FakeRepository()
        try:
            history = assistant_service.list_assistant_history(None, limit=20)
        finally:
            assistant_service.assistant_repository = original_repository

        self.assertEqual(history, [{"session_id": "s1", "message_count": 2}])

    def test_lists_messages_inside_session_in_forward_order(self):
        class FakeRepository:
            def list_sessions(self, limit=50):
                raise AssertionError("不应按会话查询")

            def list_messages(self, session_id=None, limit=50, ascending=False):
                return [{"session_id": session_id, "ascending": ascending}]

        original_repository = assistant_service.assistant_repository
        assistant_service.assistant_repository = FakeRepository()
        try:
            history = assistant_service.list_assistant_history("s1", limit=20)
        finally:
            assistant_service.assistant_repository = original_repository

        self.assertEqual(history, [{"session_id": "s1", "ascending": True}])


if __name__ == "__main__":
    unittest.main()
