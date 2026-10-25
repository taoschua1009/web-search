"""Explicit live Google and Tavily adapters for the research workflow."""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime
from email.message import EmailMessage

import httplib2
import requests
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.schemas.errors import ResearchError
from src.services.gmail_service import _decode_message
from src.services.research import search_client
from src.services.research.utils import digest
from src.services.research.web_fetcher import SafeFetcher, domain_of, normalize_url

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"


GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"


GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"


class LiveBackend:
    """Real Gmail, Calendar, web search and page fetching for one signed-in user."""

    mode = "live"

    def __init__(self, credentials, settings):
        self.credentials, self.settings = credentials, settings
        self.fetcher = SafeFetcher()
        self.credential_lock = threading.Lock()
        self.deadline = time.monotonic() + settings.deadline_seconds
        self.search_counts = {}
        self.search_lock = threading.Lock()

    def remaining(self):
        """Seconds left for one provider call; raises once the deadline passed."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ResearchError(
                "DEADLINE_EXCEEDED", "Đã hết thời gian gọi nhà cung cấp."
            )
        return min(10, remaining)

    def require(self, alternatives: list[list[str]]):
        """Ensure the credentials carry one of the scope sets and are refreshed."""
        credentials = self.credentials
        if credentials is None:
            raise ResearchError(
                "AUTH_REQUIRED", "Đăng nhập Google để dùng dữ liệu thật.", 401
            )
        if not any(credentials.has_scopes(scopes) for scopes in alternatives):
            raise ResearchError(
                "SCOPE_REQUIRED",
                "Cấp quyền Gmail/Calendar trong nút Kết nối quyền nghiên cứu.",
                403,
            )
        with self.credential_lock:
            if not credentials.valid:
                try:
                    if not credentials.refresh_token:
                        raise ValueError("No refresh token")

                    def refresh_request(*args, **kwargs):
                        kwargs["timeout"] = self.remaining()
                        return Request()(*args, **kwargs)

                    credentials.refresh(refresh_request)
                except Exception:  # noqa: BLE001 - sanitize provider errors and never retry uncertain writes
                    raise ResearchError(
                        "AUTH_REQUIRED", "Phiên Google hết hạn. Hãy đăng nhập lại.", 401
                    ) from None

    def service(self, name, version, alternatives):
        """A Google API client with a bounded timeout."""
        self.require(alternatives)
        return build(
            name,
            version,
            http=AuthorizedHttp(
                self.credentials, http=httplib2.Http(timeout=self.remaining())
            ),
            cache_discovery=False,
        )

    def execute(self, request, write=False):
        """Run a Google request, mapping failures to safe errors; writes never retry."""
        self.remaining()
        try:
            return request.execute(num_retries=0 if write else 1)
        except HttpError as error:
            status = error.resp.status
            code = (
                "ACTION_OUTCOME_UNKNOWN"
                if write and status >= 500
                else {
                    401: "AUTH_REQUIRED",
                    403: "SCOPE_REQUIRED",
                    404: "NOT_FOUND",
                    429: "PROVIDER_RATE_LIMIT",
                }.get(status, "PROVIDER_UNAVAILABLE")
            )
            message = (
                "Chưa xác định kết quả từ Google; không tự động gửi lại."
                if code == "ACTION_OUTCOME_UNKNOWN"
                else "Google không thực hiện được yêu cầu. Kiểm tra quyền truy cập."
            )
            raise ResearchError(
                code, message, 401 if status == 401 else 403 if status == 403 else 502
            ) from None
        except Exception:  # noqa: BLE001 - sanitize provider errors and never retry uncertain writes
            raise ResearchError(
                "ACTION_OUTCOME_UNKNOWN" if write else "PROVIDER_UNAVAILABLE",
                "Chưa xác định được kết quả từ Google; không tự động thử lại thao tác ghi."
                if write
                else "Không kết nối được Google.",
                502,
            ) from None

    def list_mail(self, query="in:inbox", limit=20):
        """Message headers matching a Gmail query."""
        api = (
            self.service("gmail", "v1", [[GMAIL_READ], [GMAIL_MODIFY]])
            .users()
            .messages()
        )
        result = self.execute(api.list(userId="me", q=query, maxResults=limit))
        messages = []
        errors = []
        for item in result.get("messages", []):
            try:
                raw = self.execute(
                    api.get(
                        userId="me",
                        id=item["id"],
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                )
                headers = {
                    header["name"].lower(): header["value"]
                    for header in raw.get("payload", {}).get("headers", [])
                }
                messages.append(
                    {
                        "id": raw["id"],
                        "thread_id": raw.get("threadId", ""),
                        "from": headers.get("from", ""),
                        "to": headers.get("to", ""),
                        "subject": headers.get("subject", ""),
                        "date": headers.get("date", ""),
                        "snippet": raw.get("snippet", "")[:500],
                        "demo": False,
                    }
                )
            except ResearchError as error:
                errors.append(
                    {"id": item["id"], "code": error.code, "message": error.message}
                )
        return {
            "messages": messages,
            "next_page_token": result.get("nextPageToken", ""),
            "errors": errors,
        }

    def read_mail(self, message_id):
        """One full decoded message."""
        api = (
            self.service("gmail", "v1", [[GMAIL_READ], [GMAIL_MODIFY]])
            .users()
            .messages()
        )
        raw = self.execute(api.get(userId="me", id=message_id, format="full"))
        try:
            value = _decode_message(raw)
        except (ValueError, KeyError, TypeError):
            raise ResearchError(
                "EMAIL_UNREADABLE", "Không giải mã được nội dung email này."
            ) from None
        headers = {
            header["name"].lower(): header["value"]
            for header in raw.get("payload", {}).get("headers", [])
        }
        value.update(
            rfc_message_id=headers.get("message-id", ""),
            references=headers.get("references", ""),
            demo=False,
        )
        return value

    def busy(self, start, end, timezone_name):
        """Busy intervals of the primary calendar."""
        service = self.service("calendar", "v3", [[CALENDAR_READ]])
        result = self.execute(
            service.freebusy().query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "timeZone": timezone_name,
                    "items": [{"id": "primary"}],
                }
            )
        )
        calendar = result.get("calendars", {}).get("primary")
        if calendar is None or calendar.get("errors"):
            raise ResearchError(
                "PROVIDER_UNAVAILABLE",
                "Không đọc được lịch; chưa thể kết luận lịch trống.",
                502,
            )
        try:
            intervals = [
                (
                    datetime.fromisoformat(item["start"]),
                    datetime.fromisoformat(item["end"]),
                )
                for item in calendar.get("busy", [])
            ]
            if any(
                a.utcoffset() is None or b.utcoffset() is None or b <= a
                for a, b in intervals
            ):
                raise ValueError("Invalid busy interval")
            return intervals
        except (ValueError, TypeError, KeyError):
            raise ResearchError(
                "PROVIDER_UNAVAILABLE",
                "Google trả dữ liệu lịch không hợp lệ; chưa xác định lịch trống.",
                502,
            ) from None

    def events(self, start, end, timezone_name):
        """Events of the primary calendar in a window."""
        api = self.service("calendar", "v3", [[CALENDAR_READ]]).events()
        events, token = [], None
        for _ in range(20):
            self.remaining()
            args = {
                "calendarId": "primary",
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": True,
                "maxResults": 250,
                "timeZone": timezone_name,
            }
            if token:
                args["pageToken"] = token
            result = self.execute(api.list(**args))
            events.extend(
                item
                for item in result.get("items", [])
                if item.get("status") != "cancelled"
            )
            token = result.get("nextPageToken")
            if not token:
                return events
        raise ResearchError(
            "BUDGET_EXCEEDED",
            "Lịch quá nhiều sự kiện; chưa kiểm tra hết cửa sổ yêu cầu.",
        )

    def search(self, name, topic, deadline):
        """Topic search through the configured provider."""
        if self.settings.search_provider == "tavily":
            self.reserve_search(name)
            return search_client.tavily_search(
                self.settings.tavily_key, name, topic, min(deadline, self.deadline)
            )
        if (
            self.settings.search_provider != "google"
            or not self.settings.google_key
            or not self.settings.google_engine
        ):
            raise ResearchError(
                "SEARCH_NOT_CONFIGURED",
                "Chưa cấu hình tìm kiếm Internet. Website công ty vẫn được nghiên cứu trực tiếp.",
            )
        self.reserve_search(name)
        session = requests.Session()
        session.trust_env = False
        try:
            with session.get(
                "https://customsearch.googleapis.com/customsearch/v1",
                params={
                    "key": self.settings.google_key,
                    "cx": self.settings.google_engine,
                    "q": f"{name} {topic}",
                    "num": 5,
                },
                timeout=min(self.remaining(), max(0.1, deadline - time.monotonic())),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise ResearchError(
                        "PROVIDER_UNAVAILABLE",
                        "Google Search không khả dụng cho tài khoản/cấu hình hiện tại.",
                    )
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=16384):
                    if time.monotonic() >= deadline:
                        raise ResearchError(
                            "DEADLINE_EXCEEDED", "Đã hết thời gian tìm kiếm."
                        )
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ResearchError(
                            "BUDGET_EXCEEDED", "Kết quả tìm kiếm vượt giới hạn."
                        )
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
            return [
                {
                    "url": normalize_url(item["link"]),
                    "title": str(item.get("title", ""))[:300],
                    "snippet": str(item.get("snippet", ""))[:1000],
                    "demo": False,
                }
                for item in result.get("items", [])
            ]
        except ResearchError:
            raise
        except Exception:  # noqa: BLE001 - sanitize provider errors and never retry uncertain writes
            raise ResearchError(
                "PROVIDER_UNAVAILABLE", "Không truy cập được Google Search."
            ) from None
        finally:
            session.close()

    def web_search(self, query, deadline, recent=False):
        """Free-form query for the search agent; the agent enforces its own budget."""
        if self.settings.search_provider == "tavily":
            return search_client.tavily_search(
                self.settings.tavily_key,
                "",
                query,
                min(deadline, self.deadline),
                recent=recent,
                max_results=8,
            )
        return self.search(query, "", deadline)

    def reserve_search(self, name):
        """Count a search against the per-company budget."""
        with self.search_lock:
            key = name.strip().casefold()
            if self.search_counts.get(key, 0) >= self.settings.max_search_queries:
                raise ResearchError(
                    "BUDGET_EXCEEDED", "Đã hết ngân sách tìm kiếm cho công ty này."
                )
            self.search_counts[key] = self.search_counts.get(key, 0) + 1

    def crawl(self, website, limit, deadline):
        """Crawl a company website."""
        return self.fetcher.crawl(website, limit, deadline)

    def fetch(self, url, deadline):
        """Fetch one page within its own domain."""
        return self.fetcher.fetch(url, deadline, expected_domain=domain_of(url))

    def check_write(self, kind):
        """Ensure the write scope for a send, trash or create-event action is granted."""
        if kind in {"create_event", "delete_event"}:
            self.require([[CALENDAR_EVENTS]])
        else:
            self.require(
                [[GMAIL_MODIFY]]
                if kind == "trash_email"
                else [[GMAIL_SEND], [GMAIL_MODIFY]]
            )

    def delete_event(self, payload):
        """Delete an approved event, but only if it is still the event the user approved."""
        self.check_write("delete_event")
        events = self.service("calendar", "v3", [[CALENDAR_EVENTS]]).events()
        event_id = payload["event_id"]
        try:
            current = self.execute(events.get(calendarId="primary", eventId=event_id))
        except ResearchError as error:
            if error.code == "NOT_FOUND":
                return {"event_id": event_id, "status": "already_deleted"}
            raise
        if current.get("status") == "cancelled":
            return {"event_id": event_id, "status": "already_deleted"}
        start = current.get("start", {})
        if current.get("summary", "(no title)") != payload["title"] or (
            start.get("dateTime") or start.get("date")
        ) != payload["start"]:
            raise ResearchError(
                "ACTION_CHANGED",
                "Sự kiện đã thay đổi từ lúc đề xuất. Hãy tạo đề xuất xóa mới.",
                409,
            )
        self.execute(
            events.delete(calendarId="primary", eventId=event_id, sendUpdates="none"),
            write=True,
        )
        return {"event_id": event_id, "status": "deleted"}

    def create_event(self, payload):
        """Create an approved event; a content-derived id makes a retry idempotent."""
        self.check_write("create_event")
        events = self.service("calendar", "v3", [[CALENDAR_EVENTS]]).events()
        event_id = digest({"calendar_event": payload})[:40]
        try:
            existing = self.execute(events.get(calendarId="primary", eventId=event_id))
        except ResearchError as error:
            if error.code != "NOT_FOUND":
                raise
        else:
            if existing.get("status") != "cancelled":
                return {
                    "event_id": event_id,
                    "status": "already_exists",
                    "html_link": existing.get("htmlLink", ""),
                }
        body = {
            "id": event_id,
            "summary": payload["title"],
            "start": {"dateTime": payload["start"], "timeZone": payload["timezone"]},
            "end": {"dateTime": payload["end"], "timeZone": payload["timezone"]},
        }
        if payload.get("description"):
            body["description"] = payload["description"]
        if payload.get("location"):
            body["location"] = payload["location"]
        result = self.execute(
            events.insert(calendarId="primary", body=body, sendUpdates="none"),
            write=True,
        )
        if result.get("id") != event_id:
            raise ResearchError(
                "ACTION_OUTCOME_UNKNOWN",
                "Google không xác nhận sự kiện đã tạo; kiểm tra lịch trước khi tạo lại.",
                502,
            )
        return {
            "event_id": event_id,
            "status": "created",
            "html_link": result.get("htmlLink", ""),
        }

    def send(self, payload):
        """Send an approved e-mail; provider acceptance is not delivery confirmation."""
        self.check_write("send_email")
        message = EmailMessage()
        message["To"] = ", ".join(payload["to"])
        message["Subject"] = payload["subject"]
        if payload.get("rfc_message_id"):
            message["In-Reply-To"] = payload["rfc_message_id"]
            message["References"] = payload["rfc_message_id"]
        message.set_content(payload["body"])
        body = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        if payload.get("thread_id"):
            body["threadId"] = payload["thread_id"]
        service = self.service("gmail", "v1", [[GMAIL_SEND], [GMAIL_MODIFY]])
        result = self.execute(
            service.users().messages().send(userId="me", body=body), write=True
        )
        if not result.get("id"):
            raise ResearchError(
                "ACTION_OUTCOME_UNKNOWN",
                "Google không trả mã thư đã gửi; kiểm tra thư đã gửi trước khi tạo hành động khác.",
                502,
            )
        return {
            "message_id": result["id"],
            "status": "provider_accepted",
            "delivery_confirmed": False,
        }

    def trash(self, payload):
        """Move an approved message to Trash (never a permanent delete)."""
        service = self.service("gmail", "v1", [[GMAIL_MODIFY]])
        api = service.users().messages()
        current = self.execute(
            api.get(userId="me", id=payload["message_id"], format="minimal")
        )
        if "TRASH" in current.get("labelIds", []):
            return {"message_id": payload["message_id"], "status": "already_in_trash"}
        result = self.execute(
            api.trash(userId="me", id=payload["message_id"]), write=True
        )
        if result.get("id") != payload["message_id"]:
            raise ResearchError(
                "ACTION_OUTCOME_UNKNOWN",
                "Chưa xác định kết quả chuyển thư vào Thùng rác.",
                502,
            )
        return {"message_id": result["id"], "status": "trashed"}
