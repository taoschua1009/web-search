"""Encrypted on-disk copy of login sessions, so a server restart does not log users out.

Google credentials (including the refresh token) are encrypted with Fernet. The key
comes from ``LOGIN_SESSION_KEY`` or, when unset, from a ``0600`` key file next to the
data; both live in the git-ignored ``.runtime`` folder. A damaged or foreign file is
ignored, never trusted.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class EncryptedSessionStore:
    """Reads and atomically rewrites one encrypted JSON file of session records."""

    def __init__(self, path: Path, key_file: Path, env_key: str = ""):
        self.path, self.key_file, self.env_key = path, key_file, env_key

    def _fernet(self, create: bool) -> Fernet | None:
        if self.env_key:
            return Fernet(self.env_key.encode())
        if self.key_file.exists():
            return Fernet(self.key_file.read_bytes().strip())
        if not create:
            return None
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        descriptor = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
        return Fernet(key)

    def load(self) -> dict[str, dict]:
        """Decrypted records, or an empty dict when the file is absent or unusable."""
        try:
            fernet = self._fernet(create=False)
            if fernet is None or not self.path.exists():
                return {}
            data = json.loads(fernet.decrypt(self.path.read_bytes()))
        except (OSError, ValueError, InvalidToken):
            logger.warning("Ignoring unreadable login session store.")
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, records: dict[str, dict]) -> None:
        """Replace the file atomically with the encrypted records (mode 0600)."""
        fernet = self._fernet(create=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = fernet.encrypt(json.dumps(records).encode())
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".sessions-"
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
