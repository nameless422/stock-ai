import unittest

from app.core.strategy_reviewer import review_strategy_code


VALID_CODE = """
def run_strategy(context):
    stock = context["stock"]
    daily = context["snapshots"]["daily"]
    indicators = context["indicators"]["daily"]
    if not daily.get("enough_data"):
        return {"pass": False, "reason": "日线数据不足"}
    ma5 = indicators["ma5"][-1]
    passed = daily["latest_close"] > ma5
    return {
        "pass": passed,
        "reason": f"{stock['name']} 收盘价{'高于' if passed else '低于'} MA5",
        "score": 80 if passed else 20,
        "metrics": {"latest_close": daily["latest_close"], "ma5": ma5},
    }
""".strip()


class StrategyReviewTest(unittest.TestCase):
    def test_review_runs_fixture_cases_for_valid_strategy(self):
        review = review_strategy_code(VALID_CODE)

        self.assertTrue(review["ok"], review)
        self.assertEqual(review["summary"]["case_total"], 3)
        self.assertEqual(review["summary"]["case_passed"], 3)
        self.assertGreaterEqual(len(review["tests"]), 3)
        self.assertTrue(review["analysis"]["context_paths"])
        self.assertIn("pass", review["analysis"]["return_keys"])
        self.assertIn("reason", review["analysis"]["return_keys"])

    def test_review_reports_static_errors_and_failed_cases(self):
        review = review_strategy_code(
            "import os\n\n"
            "def run_strategy(context):\n"
            "    return {'pass': True, 'reason': os.getcwd()}\n"
        )

        self.assertFalse(review["ok"])
        self.assertTrue(review["findings"])
        self.assertTrue(any(item["severity"] == "error" for item in review["findings"]))
        self.assertEqual(review["summary"]["case_passed"], 0)


if __name__ == "__main__":
    unittest.main()
