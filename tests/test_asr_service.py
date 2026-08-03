import unittest

from app.services.asr_service import _clean_transcript


class ASRServiceTest(unittest.TestCase):
    def test_cleans_common_traditional_chinese_transcript(self):
        self.assertEqual(_clean_transcript("幫我選出放量突破的股票。"), "帮我选出放量突破的股票。")


if __name__ == "__main__":
    unittest.main()
