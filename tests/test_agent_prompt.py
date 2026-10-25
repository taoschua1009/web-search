"""The chat agent must know today's date to resolve relative dates itself."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.agents.agent import SYSTEM_PROMPT, _system_prompt
from src.configs.config import CALENDAR_TIMEZONE
from src.services import calendar_service


class SystemPromptTests(unittest.TestCase):
    def test_prompt_carries_the_current_date_and_keeps_the_configured_text(self):
        prompt = _system_prompt()
        self.assertTrue(prompt.startswith(SYSTEM_PROMPT))
        today = datetime.now(ZoneInfo(CALENDAR_TIMEZONE)).strftime("%Y-%m-%d")
        self.assertIn(today, prompt)
        self.assertIn(CALENDAR_TIMEZONE, prompt)

    def test_missing_calendar_permission_explains_how_to_grant_it(self):
        from google.oauth2.credentials import Credentials

        from src.services.gmail_service import request_credentials

        token = request_credentials.set(Credentials(token="t", scopes=["gmail"]))
        try:
            with self.assertRaisesRegex(PermissionError, r"/auth/login\?research=1"):
                calendar_service._get_service()
        finally:
            request_credentials.reset(token)


if __name__ == "__main__":
    unittest.main()
