import os
import unittest
from unittest.mock import patch

from app.services import strategy_service


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            "def run_strategy(context):\n"
                            "    return {'pass': False, 'reason': '测试策略'}\n"
                        )
                    }
                }
            ]
        }


class FakeClient:
    posts = []

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers or {}, "json": json or {}})
        return FakeResponse()


class DeepSeekStrategyGenerationTest(unittest.TestCase):
    def setUp(self):
        FakeClient.posts = []

    def test_build_context_prefers_deepseek_over_existing_llm_keys(self):
        env = {
            "DEEPSEEK_API_KEY": "deepseek-key",
            "LLM_API_KEY": "llm-key",
            "OPENAI_API_KEY": "openai-key",
        }
        with patch.dict(os.environ, env, clear=True):
            context = strategy_service.build_strategy_generation_context("5日线上穿10日线")

        self.assertEqual(context["provider"], "deepseek")
        self.assertEqual(context["base_url"], "https://api.deepseek.com")
        self.assertEqual(context["model"], "deepseek-v4-flash")

    def test_generate_strategy_code_calls_deepseek_chat_completion(self):
        env = {"DEEPSEEK_API_KEY": "deepseek-key"}
        with patch.dict(os.environ, env, clear=True), patch.object(strategy_service.httpx, "Client", FakeClient):
            code = strategy_service.generate_strategy_code("生成一个测试策略")

        self.assertIn("def run_strategy(context):", code)
        self.assertEqual(len(FakeClient.posts), 1)
        request = FakeClient.posts[0]
        self.assertEqual(request["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer deepseek-key")
        self.assertEqual(request["json"]["model"], "deepseek-v4-flash")
        self.assertEqual(request["json"]["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()
