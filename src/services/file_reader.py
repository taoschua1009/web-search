"""
File Reader Service - Convert files to Markdown using MarkItDown.

The model chooses the path, so this is a trust boundary: only files inside configured
folders are readable, symlinks are resolved first, and credential-like files are always
refused (OWASP: scoped tools with path allowlists and sensitive-pattern blocklists).
"""

import fnmatch
import os
from pathlib import Path

from markitdown import MarkItDown

from src.configs.config import READ_FILE_ALLOWED_DIRS

MAX_CHARS = 15000
MAX_BYTES = 25 * 1024 * 1024

DENIED_NAMES = (
    ".env", ".env.*", "*.env", "*.key", "*.pem", "*.p12", "*.pfx", "*.crt", "*.jks",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.kdbx", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", "client_secret*", "credential*", "token*.json", "*_token.json",
    "service_account*", "service-account*", "*.sqlite", "*.sqlite3", "*.db", "*.enc",
    "login_sessions*", "secrets.*", "*.keychain*", "known_hosts",
)
DENIED_DIRS = {".ssh", ".aws", ".gnupg", ".git", ".runtime", ".kube", ".docker", ".config", "secrets"}


def _refuse() -> PermissionError:
    return PermissionError(
        "[FILE] That file is outside the folders the assistant may read, or is a protected file."
    )


def resolve_readable(file_path: str, allowed_dirs=None) -> Path:
    """The real path of a file the tool may read; raises PermissionError otherwise."""
    roots = [Path(item).expanduser().resolve() for item in (allowed_dirs or READ_FILE_ALLOWED_DIRS)]
    try:
        path = Path(file_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise FileNotFoundError("[FILE] File not found.") from None
    if not any(path.is_relative_to(root) for root in roots):
        raise _refuse()
    names = [path.name.lower()]
    if any(fnmatch.fnmatch(name, pattern) for name in names for pattern in DENIED_NAMES):
        raise _refuse()
    if any(part.lower() in DENIED_DIRS for part in path.parts):
        raise _refuse()
    if not path.is_file():
        raise _refuse()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"[FILE] File is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    return path


def read_file(file_path: str) -> dict:
    """Read a local file and convert its content to Markdown."""
    path = resolve_readable(file_path)
    text = MarkItDown().convert(str(path)).text_content

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n... [truncated, total {len(text)} chars]"

    return {
        "file_name": os.path.basename(path),
        "content": text,
    }
