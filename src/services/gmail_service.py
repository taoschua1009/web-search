"""Read Gmail through user OAuth, with no mailbox write operations."""

from __future__ import annotations

import base64
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from email.message import EmailMessage
from email import policy
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import tempfile

from opentelemetry import trace
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.configs.config import (
    GOOGLE_GMAIL_CLIENT_FILE,
    GOOGLE_GMAIL_OAUTH_PORT,
    GOOGLE_GMAIL_TOKEN_FILE,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LOGIN_HINT = "Run `.venv/bin/python -m src.services.gmail_service` to connect Gmail, then retry."
MAX_BODY_CHARS = 20_000
request_credentials: ContextVar[Credentials | None] = ContextVar("gmail_credentials", default=None)
_message_cache: ContextVar[dict[str, dict] | None] = ContextVar("gmail_message_cache", default=None)
tracer = trace.get_tracer(__name__)


@contextmanager
def gmail_cache_scope():
    """Share prefetched messages only inside one agent run, never across users."""
    token = _message_cache.set({})
    try:
        yield
    finally:
        _message_cache.reset(token)


def _save_credentials(creds: Credentials) -> None:
    """Replace the token atomically; the temporary file is owner-only too."""
    target = Path(GOOGLE_GMAIL_TOKEN_FILE)
    fd, temporary = tempfile.mkstemp(prefix=".gmail-token-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(creds.to_json())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def login() -> str:
    """Ask the user for read-only access in their browser, then save a token."""
    if not Path(GOOGLE_GMAIL_CLIENT_FILE).is_file():
        raise FileNotFoundError(
            f"OAuth client file missing: {GOOGLE_GMAIL_CLIENT_FILE}. "
            "Download an OAuth client from Google Cloud Console first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_GMAIL_CLIENT_FILE, SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="127.0.0.1",
        port=GOOGLE_GMAIL_OAUTH_PORT,
        redirect_uri_trailing_slash=False,
        prompt="consent",
        access_type="offline",
        timeout_seconds=300,
        success_message="Gmail connected. You can close this tab and return to the agent.",
    )
    if not creds or not set(SCOPES).issubset(creds.granted_scopes or creds.scopes or []):
        raise PermissionError("Gmail read permission was not granted. " + LOGIN_HINT)
    _save_credentials(creds)
    return "Gmail connected with read-only access. Return to http://localhost:9004."


def _has_read_access(credentials: Credentials) -> bool:
    """A modify grant includes read access without requesting redundant scopes."""
    return credentials.has_scopes(SCOPES) or credentials.has_scopes(["https://www.googleapis.com/auth/gmail.modify"])


def _get_credentials() -> Credentials:
    browser_credentials = request_credentials.get()
    if browser_credentials is not None:
        if not _has_read_access(browser_credentials):
            raise PermissionError("Sign out and sign in again to grant Gmail read permission.")
        if not browser_credentials.valid:
            if not browser_credentials.refresh_token:
                raise PermissionError("Gmail authorization expired. Sign out and sign in again.")
            try:
                browser_credentials.refresh(Request())
            except RefreshError as error:
                raise PermissionError("Gmail authorization was revoked. Sign out and sign in again.") from error
        return browser_credentials
    token = Path(GOOGLE_GMAIL_TOKEN_FILE)
    if not token.is_file():
        raise PermissionError("Gmail is not connected. " + LOGIN_HINT)
    try:
        info = json.loads(token.read_text(encoding="utf-8"))
        # Do not overwrite the recorded scopes with requested scopes: Calendar
        # tokens must never be mistaken for a Gmail authorization.
        creds = Credentials.from_authorized_user_info(info)
    except (ValueError, KeyError) as error:
        raise PermissionError("Gmail token is invalid. " + LOGIN_HINT) from error
    if not _has_read_access(creds):
        raise PermissionError("Gmail read permission is missing. " + LOGIN_HINT)
    if not creds.valid:
        if not creds.refresh_token:
            raise PermissionError("Gmail authorization expired. " + LOGIN_HINT)
        try:
            creds.refresh(Request())
        except RefreshError as error:
            raise PermissionError("Gmail authorization expired or was revoked. " + LOGIN_HINT) from error
        _save_credentials(creds)
    return creds


def _get_service():
    # A fresh transport avoids sharing httplib2 connections across API threads.
    return build("gmail", "v1", credentials=_get_credentials(), cache_discovery=False)


def _execute(request):
    try:
        return request.execute()
    except HttpError as error:
        _raise_http_error(error)


def _raise_http_error(error: HttpError):
    if error.resp.status == 401:
        raise PermissionError("Gmail authorization failed. " + LOGIN_HINT) from error
    if error.resp.status == 403:
        raise PermissionError(
            "Gmail API access was denied. Enable Gmail API in the OAuth client's "
            "Google Cloud project and check the account's Gmail permission. " + LOGIN_HINT
        ) from error
    if error.resp.status == 404:
        raise LookupError("Email not found. Search Gmail again for a current message ID.") from error
    raise error


def _batch_get_messages(service, message_ids: list[str]) -> tuple[dict, list[dict]]:
    """Fetch a page's full payloads in one HTTP batch, preserving ID mapping."""
    responses, failures = {}, {}

    def collect(request_id, response, exception):
        if exception is not None:
            failures[request_id] = exception
        elif response is None or response.get("id") != request_id or "payload" not in response:
            failures[request_id] = ValueError("Gmail returned an invalid message.")
        else:
            responses[request_id] = response

    with tracer.start_as_current_span("gmail.batch_get") as span:
        span.set_attribute("gmail.batch_size", len(message_ids))
        batch = service.new_batch_http_request(callback=collect)
        messages_api = service.users().messages()
        for message_id in message_ids:
            batch.add(messages_api.get(userId="me", id=message_id, format="full"), request_id=message_id)
        _execute(batch)
        errors = []
        for message_id in message_ids:
            if message_id in responses:
                continue
            error = failures.get(message_id)
            if isinstance(error, HttpError) and error.resp.status in {401, 403}:
                _raise_http_error(error)
            status = error.resp.status if isinstance(error, HttpError) else None
            errors.append({
                "id": message_id,
                "error": "Email no longer exists." if status == 404 else "Could not fetch this email. Retry the search.",
            })
        span.set_attribute("gmail.failed_count", len(errors))
        return responses, errors


def list_messages(query: str = "in:inbox", max_results: int = 10, page_token: str = "") -> dict:
    """Search Gmail and return headers/snippets plus a continuation token."""
    if type(max_results) is not int or not 1 <= max_results <= 20:
        raise ValueError("max_results must be an integer between 1 and 20.")
    service = _get_service()
    messages_api = service.users().messages()
    arguments = {"userId": "me", "q": query, "maxResults": max_results}
    if page_token:
        arguments["pageToken"] = page_token
    result = _execute(messages_api.list(**arguments))
    message_ids = list(dict.fromkeys(item["id"] for item in result.get("messages", [])))
    responses, errors = _batch_get_messages(service, message_ids) if message_ids else ({}, [])
    cache = _message_cache.get()
    if cache is not None:
        cache.update(responses)
    messages = []
    for message_id in message_ids:
        if message_id not in responses:
            continue
        message = responses[message_id]
        headers = {
            header["name"].lower(): header["value"]
            for header in message.get("payload", {}).get("headers", [])
        }
        messages.append({
            "id": message["id"],
            "thread_id": message.get("threadId", ""),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", "(no subject)"),
            "date": headers.get("date", ""),
            "snippet": unescape(message.get("snippet", "")),
            "unread": "UNREAD" in message.get("labelIds", []),
        })
    return {
        "query": query,
        "messages": messages,
        "count": len(messages),
        "next_page_token": result.get("nextPageToken", ""),
        "errors": errors,
    }


class _HTMLText(HTMLParser):
    """Extract readable text without including scripts, styles or HTML tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def _payload_message(payload: dict, message_id: str) -> EmailMessage:
    """Rebuild the MIME tree from FULL data without downloading attachments."""
    message = EmailMessage(policy=policy.default)
    for header in payload.get("headers", []):
        try:
            # Gmail can return folded headers. Unfold them before assigning
            # through EmailMessage, which rejects embedded CR/LF characters.
            message[header["name"]] = " ".join(header["value"].splitlines())
        except ValueError:
            if header["name"] not in message:
                raise
    if "Content-Type" not in message:
        message.set_type(payload.get("mimeType", "text/plain"))
    filename = payload.get("filename")
    if filename and "Content-Disposition" not in message:
        message.add_header("Content-Disposition", "attachment", filename=filename)
    if filename or message.get_filename() or message.get_content_disposition() == "attachment":
        message.set_payload("")
    elif payload.get("parts"):
        message.set_payload([_payload_message(part, message_id) for part in payload["parts"]])
    elif message.get_content_type() in {"text/plain", "text/html"}:
        body = payload.get("body", {})
        data = body.get("data", "")
        # Large text bodies can be stored separately, just like attachments.
        if not data and body.get("attachmentId"):
            data = _execute(_get_service().users().messages().attachments().get(
                userId="me", messageId=message_id, id=body["attachmentId"],
            )).get("data", "")
            body["data"] = data
            body.pop("attachmentId", None)
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        # Gmail has already decoded the original MIME transfer encoding.
        # Normalize it so email.get_content() does not decode it twice.
        if "Content-Transfer-Encoding" in message:
            del message["Content-Transfer-Encoding"]
        message["Content-Transfer-Encoding"] = "base64"
        message.set_payload(base64.b64encode(decoded).decode("ascii"))
    else:
        message.set_payload("")
    return message


def _decode_message(response: dict) -> dict:
    if "payload" not in response:
        raise ValueError("Gmail returned no message content.")
    message_id = response["id"]
    message = _payload_message(response["payload"], message_id)
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, UnicodeError):
            text = body.get_payload(decode=True).decode("utf-8", errors="replace")
        if body.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            text = "\n".join(line.strip() for line in "".join(parser.chunks).splitlines() if line.strip())
    return {
        "id": response.get("id", message_id),
        "thread_id": response.get("threadId", ""),
        "from": str(message.get("From", "")),
        "to": str(message.get("To", "")),
        "subject": str(message.get("Subject", "(no subject)")),
        "date": str(message.get("Date", "")),
        "body": text[:MAX_BODY_CHARS],
        "body_truncated": len(text) > MAX_BODY_CHARS,
        "attachments": [str(part.get_filename()) for part in message.walk() if part.get_filename()],
    }


def read_message(message_id: str) -> dict:
    """Read a prefetched FULL message or fetch it once on a cache miss."""
    if not message_id.strip():
        raise ValueError("message_id must not be empty.")
    cache = _message_cache.get()
    with tracer.start_as_current_span("gmail.read_message") as span:
        cached = cache is not None and message_id in cache
        span.set_attribute("gmail.cache_hit", cached)
        response = cache[message_id] if cached else _execute(
            _get_service().users().messages().get(userId="me", id=message_id, format="full")
        )
        result = _decode_message(response)
        if cache is not None:
            cache[message_id] = response
        return result


def search_and_read_messages(query: str = "in:inbox", max_results: int = 10, page_token: str = "") -> dict:
    """Return a page of complete email bodies using one search and one batch."""
    # Reuse the run's cache, and also work efficiently when called directly.
    scope = gmail_cache_scope() if _message_cache.get() is None else nullcontext()
    with scope:
        result = list_messages(query=query, max_results=max_results, page_token=page_token)
        messages = []
        errors = list(result["errors"])
        for summary in result["messages"]:
            try:
                content = read_message(summary["id"])
            except (ValueError, LookupError):
                errors.append({"id": summary["id"], "error": "Could not read this email's text. Retry this message separately."})
            else:
                messages.append({**summary, **content})
        return {**result, "messages": messages, "count": len(messages), "errors": errors}


if __name__ == "__main__":
    from argparse import ArgumentParser

    ArgumentParser(description="Connect Gmail with read-only OAuth access.").parse_args()
    print(login())
