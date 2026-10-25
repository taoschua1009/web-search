"""Bounded public HTTP retrieval with DNS pinning and safe source extraction."""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from src.schemas.errors import ResearchError
from src.services.research.utils import digest, utcnow


def normalize_url(value: str) -> str:
    """Validate a public HTTP(S) URL and strip tracking parameters."""
    if (
        not value
        or len(value) > 2048
        or any(ord(char) < 33 or char in "<>\\`" for char in value)
    ):
        raise ResearchError("URL_BLOCKED", "Đường dẫn không hợp lệ.")
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode().lower()
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ResearchError("URL_BLOCKED", "Đường dẫn không hợp lệ.") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
    ):
        raise ResearchError(
            "URL_BLOCKED",
            "Chỉ đọc website HTTP(S) công khai, không có thông tin đăng nhập.",
        )
    if port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise ResearchError("URL_BLOCKED", "Cổng website không được hỗ trợ.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ResearchError("URL_BLOCKED", "Không truy cập địa chỉ nội bộ.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ResearchError("URL_BLOCKED", "Tên miền không hợp lệ.") from None
    else:
        if not address.is_global:
            raise ResearchError("URL_BLOCKED", "Không truy cập địa chỉ nội bộ.")
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid"}
        ]
    )
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            query,
            "",
        )
    )


def domain_of(url: str) -> str:
    """Host of a URL without a leading ``www.``."""
    return (urlsplit(normalize_url(url)).hostname or "").removeprefix("www.")


def public_addresses(host: str, port: int):
    """Resolve a host and keep only globally routable addresses."""
    try:
        addresses = list(
            dict.fromkeys(
                info[4][0]
                for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
    except OSError:
        raise ResearchError("FETCH_FAILED", "Không phân giải được tên miền.") from None
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise ResearchError(
            "URL_BLOCKED", "Website phân giải tới địa chỉ không công khai."
        )
    return addresses


class PageParser(HTMLParser):
    """Collects visible text, links, title and publication date from HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.title = [], [], []
        self.skip = 0
        self.in_title = False
        self.published_at = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if not self.skip and tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "meta" and attrs.get("property", attrs.get("name", "")).lower() in {
            "article:published_time",
            "datepublished",
            "pubdate",
        }:
            raw = attrs.get("content", "")
            with contextlib.suppress(ValueError):
                self.published_at = datetime.fromisoformat(raw).isoformat()
        if tag in {"p", "br", "li", "h1", "h2", "h3", "div", "section", "tr"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
        if tag in {"p", "li", "h1", "h2", "h3", "div", "section", "tr"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)


CRAWL_GROUPS = (
    r"about|company|gioi-thieu",
    r"product|service|solution|san-pham|dich-vu",
    r"team|leadership|management|founder|people|doi-ngu",
    r"investor|financial|annual|report|revenue|tai-chinh",
    r"contact|location|office|lien-he",
    r"news|press|media|tin-tuc",
    r"price|pricing|gia-ban",
    r"partner|customer|project|case-stud|doi-tac|du-an",
)


def crawl_group(url):
    """Priority group of a URL for the website crawl (lower is more useful)."""
    path = urlsplit(url).path
    return next(
        (
            index
            for index, pattern in enumerate(CRAWL_GROUPS)
            if re.search(pattern, path, re.IGNORECASE)
        ),
        len(CRAWL_GROUPS),
    )


CRAWL_EXTRA = (r"job|career|tuyen-dung|recruit",)


class SafeFetcher:
    """Connect to a validated public IP while verifying TLS for the original host."""

    def __init__(self, timeout: float = 10, max_bytes: int = 1_000_000):
        self.timeout, self.max_bytes = timeout, max_bytes

    def _request(self, url: str, deadline: float):
        parsed = urlsplit(url)
        host = parsed.hostname
        port = 443 if parsed.scheme == "https" else 80
        addresses = public_addresses(host, port)
        remaining = min(self.timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise ResearchError("DEADLINE_EXCEEDED", "Đã hết thời gian đọc website.")
        connection = http.client.HTTPConnection(host, port, timeout=remaining)
        try:
            sock = socket.create_connection((addresses[0], port), timeout=remaining)
            if parsed.scheme == "https":
                try:
                    sock = ssl.create_default_context().wrap_socket(
                        sock, server_hostname=host
                    )
                except BaseException:
                    sock.close()
                    raise
            connection.sock = sock
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "CompanyResearch/1.0",
                    "Accept": "text/html,text/plain",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            headers = {key.lower(): val for key, val in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                return response.status, headers, b""
            if response.status != 200:
                raise ResearchError(
                    "FETCH_FAILED",
                    f"Website không trả nội dung đọc được (HTTP {response.status}).",
                )
            if headers.get("content-encoding", "identity").lower() not in {
                "",
                "identity",
            }:
                raise ResearchError(
                    "FETCH_FAILED", "Website trả nội dung nén không được hỗ trợ."
                )
            content_type = headers.get("content-type", "").lower()
            if not any(
                kind in content_type
                for kind in ("text/html", "text/plain", "application/xhtml+xml")
            ):
                raise ResearchError(
                    "FETCH_FAILED", "Chỉ hỗ trợ trang HTML hoặc văn bản công khai."
                )
            if int(headers.get("content-length", "0")) > self.max_bytes:
                raise ResearchError(
                    "BUDGET_EXCEEDED", "Trang vượt giới hạn kích thước."
                )
            chunks, length = [], 0
            while True:
                remaining = min(self.timeout, deadline - time.monotonic())
                if remaining <= 0:
                    raise ResearchError(
                        "DEADLINE_EXCEEDED", "Đã hết thời gian đọc website."
                    )
                sock.settimeout(remaining)
                chunk = response.read(min(16384, self.max_bytes + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > self.max_bytes:
                    raise ResearchError(
                        "BUDGET_EXCEEDED", "Trang vượt giới hạn kích thước."
                    )
            return response.status, headers, b"".join(chunks)
        finally:
            connection.close()

    def fetch(self, url: str, deadline: float, expected_domain: str | None = None):
        """Fetch one public page (limited redirects, size and time) and extract its text."""
        url = normalize_url(url)
        for _ in range(4):
            if expected_domain and domain_of(url) != expected_domain:
                raise ResearchError(
                    "URL_BLOCKED", "Chuyển hướng ra ngoài website đã chọn."
                )
            for attempt in range(2):
                try:
                    status, headers, raw = self._request(url, deadline)
                    break
                except ResearchError:
                    raise
                except (OSError, http.client.HTTPException, ValueError):
                    if attempt == 1 or time.monotonic() >= deadline:
                        raise ResearchError(
                            "FETCH_FAILED",
                            "Không đọc được website. Có thể trang bị chặn hoặc cần đăng nhập.",
                        ) from None
            if status in {301, 302, 303, 307, 308}:
                url = normalize_url(urljoin(url, headers.get("location", "")))
                continue
            parser = PageParser()
            match = re.search(
                r"charset=([\w-]+)", headers.get("content-type", ""), re.IGNORECASE
            )
            encoding = match.group(1) if match else "utf-8"
            try:
                text = raw.decode(encoding, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            parser.feed(text)
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in "".join(parser.text).splitlines()
            ]
            extracted_text = "\n".join(line for line in lines if line)
            body = extracted_text[:30000]
            if len(body) < 40:
                raise ResearchError(
                    "FETCH_FAILED",
                    "Trang không có đủ văn bản đọc được; có thể cần JavaScript.",
                )
            links = []
            for link in parser.links[:300]:
                try:
                    links.append(normalize_url(urljoin(url, link)))
                except ResearchError:
                    continue
            return {
                "id": "src-" + digest([url, body])[:20],
                "url": url,
                "title": " ".join(parser.title).strip()[:300] or url,
                "kind": "website",
                "retrieved_at": utcnow(),
                "published_at": parser.published_at,
                "text": body,
                "access": "partial_text"
                if len(extracted_text) > len(body)
                else "full_text",
                "demo": False,
                "links": list(dict.fromkeys(links)),
            }
        raise ResearchError("FETCH_FAILED", "Website chuyển hướng quá nhiều lần.")

    def crawl(self, website: str, limit: int, deadline: float):
        """Breadth-first crawl of one site, one page per topic group first."""
        first = normalize_url(website)
        host = domain_of(first)
        queue, seen, pages, errors = deque([(first, 0)]), set(), [], []
        group_counts = {}
        while queue and len(seen) < limit:
            if time.monotonic() >= deadline:
                errors.append(
                    {
                        "code": "DEADLINE_EXCEEDED",
                        "message": "Đã hết thời gian nghiên cứu website.",
                    }
                )
                break
            candidate = min(
                queue,
                key=lambda item: (
                    group_counts.get(crawl_group(item[0]), 0),
                    crawl_group(item[0]),
                    item[1],
                ),
            )
            queue.remove(candidate)
            url, depth = candidate
            if url in seen:
                continue
            seen.add(url)
            group = crawl_group(url)
            group_counts[group] = group_counts.get(group, 0) + 1
            try:
                page = self.fetch(url, deadline, expected_domain=host)
                pages.append(page)
                if depth < 2:
                    candidates = [
                        link
                        for link in page["links"]
                        if domain_of(link) == host
                        and crawl_group(link) < len(CRAWL_GROUPS)
                    ]
                    queue.extend(
                        (link, depth + 1) for link in candidates if link not in seen
                    )
            except ResearchError as error:
                errors.append(
                    {"code": error.code, "message": error.message, "url": url}
                )
        return pages, errors


def url_key(url):
    """Identity of a page for de-duplication: domain, path and query."""
    normalized = normalize_url(url)
    parsed = urlsplit(normalized)
    return (domain_of(normalized), parsed.path.rstrip("/"), parsed.query)


def extended_crawl_group(url):
    """Crawl priority including the careers pages the plain crawler skips."""
    path = urlsplit(url).path
    for offset, pattern in enumerate(CRAWL_EXTRA):
        if re.search(pattern, path, re.IGNORECASE):
            return len(CRAWL_GROUPS) + offset
    return crawl_group(url)
