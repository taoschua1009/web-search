"""Login sessions survive a restart: encrypted storage, expiry, ownership and logout."""

import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from google.oauth2.credentials import Credentials

from src.services import web_auth
from src.services.session_store import EncryptedSessionStore

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]


def credentials(client_id="app-client", refresh="refresh-secret"):
    return Credentials(
        token="access-secret",
        refresh_token=refresh,
        client_id=client_id,
        client_secret="client-secret",
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
        expiry=datetime.utcnow() + timedelta(hours=1),
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.store = EncryptedSessionStore(folder / "s.enc", folder / "s.key")

    def test_records_round_trip_and_nothing_is_readable_on_disk(self):
        self.store.save({"id1": {"email": "a@example.com", "token": "refresh-secret"}})
        raw = self.store.path.read_bytes()
        self.assertNotIn(b"a@example.com", raw)
        self.assertNotIn(b"refresh-secret", raw)
        self.assertEqual(self.store.load()["id1"]["email"], "a@example.com")
        for path in (self.store.path, self.store.key_file):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_missing_corrupt_or_foreign_files_are_ignored(self):
        self.assertEqual(self.store.load(), {})
        self.store.save({"id1": {"x": 1}})
        self.store.path.write_bytes(b"not encrypted")
        self.assertEqual(self.store.load(), {})
        other = EncryptedSessionStore(
            self.store.path, Path(self.temp.name) / "other.key"
        )
        other.save({"id2": {"x": 2}})
        self.assertEqual(self.store.load(), {})  # written with a different key

    def test_key_from_the_environment_is_used_instead_of_a_key_file(self):
        from cryptography.fernet import Fernet

        keyed = EncryptedSessionStore(
            Path(self.temp.name) / "k.enc",
            Path(self.temp.name) / "k.key",
            Fernet.generate_key().decode(),
        )
        keyed.save({"id": {"v": 1}})
        self.assertEqual(keyed.load(), {"id": {"v": 1}})
        self.assertFalse((Path(self.temp.name) / "k.key").exists())


class WebAuthPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        for target, value in (
            (
                "_session_store",
                EncryptedSessionStore(folder / "s.enc", folder / "s.key"),
            ),
            ("_web_client_id", lambda: "app-client"),
        ):
            patcher = patch.object(web_auth, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        web_auth._sessions.clear()
        self.addCleanup(web_auth._sessions.clear)

    def sign_in(self, creds=None, hours=1):
        web_auth._sessions["sid"] = web_auth.LoginSession(
            "owner@example.com", creds or credentials(), time.time() + hours * 3600
        )
        web_auth._persist()

    def test_a_restart_restores_the_session_with_its_refresh_token_and_scopes(self):
        self.sign_in()
        web_auth._sessions.clear()  # what a server restart does
        self.assertEqual(web_auth.restore_sessions(), 1)
        session = web_auth.get_session("sid")
        self.assertEqual(session.email, "owner@example.com")
        self.assertEqual(session.credentials.refresh_token, "refresh-secret")
        self.assertTrue(session.credentials.has_scopes(SCOPES))

    def test_expired_foreign_and_demo_sessions_are_not_kept(self):
        self.sign_in(hours=-1)
        web_auth._sessions.clear()
        self.assertEqual(web_auth.restore_sessions(), 0)
        self.sign_in(creds=credentials(client_id="someone-else"))
        self.sign_in(creds=credentials(refresh=None))
        web_auth._sessions.clear()
        self.assertEqual(web_auth.restore_sessions(), 0)
        web_auth._sessions["demo"] = web_auth.LoginSession(
            "d@example.invalid", None, time.time() + 60, demo=True
        )
        web_auth._persist()
        web_auth._sessions.clear()
        self.assertEqual(web_auth.restore_sessions(), 0)

    def test_logout_removes_the_saved_session(self):
        self.sign_in()
        web_auth.logout("sid")
        self.assertEqual(web_auth.restore_sessions(), 0)

    def test_only_gmail_is_required_so_declining_calendar_still_signs_in(self):
        gmail_only = credentials()
        gmail_only._scopes = ["https://www.googleapis.com/auth/gmail.modify"]
        self.assertTrue(web_auth._has_gmail_access(gmail_only))
        calendar_only = credentials()
        calendar_only._scopes = ["https://www.googleapis.com/auth/calendar.events"]
        self.assertFalse(web_auth._has_gmail_access(calendar_only))


if __name__ == "__main__":
    unittest.main()
