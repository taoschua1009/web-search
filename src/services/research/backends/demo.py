"""Isolated, clearly labeled demo backends; every operation stays in process."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.schemas.errors import ResearchError
from src.services.research.utils import digest, next_week, utcnow
from src.services.research.web_fetcher import domain_of, normalize_url


class DemoBackend:
    """Invented companies only. Every provider operation stays in process."""

    mode = "demo"
    dataset = "synthetic"
    COMPANIES = (
        (
            "atlas.example.test",
            "Atlas Labs",
            "AI và phần mềm",
            "Giải pháp xử lý tài liệu và tìm kiếm tri thức",
            "Đội ngũ giả lập khoảng 12–20 người",
            "Gói demo 2.000.000 VND/tháng",
        ),
        (
            "meridian.example.test",
            "Meridian Tools",
            "Thiết bị công nghiệp",
            "Thiết bị và dịch vụ bảo trì nhà máy",
            "Đội ngũ giả lập khoảng 80–120 người",
            "Báo giá theo yêu cầu",
        ),
        (
            "northstar.example.test",
            "Northstar Coffee",
            "Thực phẩm và bán lẻ",
            "Cà phê và chương trình đối tác cửa hàng",
            "Mạng lưới giả lập 8 cửa hàng",
            "Gói đối tác giả lập từ 5.000.000 VND",
        ),
    )

    def __init__(self, settings, owner="", store=None, now=None):
        self.settings, self.owner, self.store = settings, owner, store
        self.now = now or datetime.now(UTC)
        monday, _ = next_week(self.now, "Asia/Ho_Chi_Minh")
        self.monday = monday
        self.messages = {}
        for index, (domain, name, *_) in enumerate(self.COMPANIES):
            start = monday.replace(hour=10 + index)
            message_id = f"demo-{index + 1}"
            self.messages[message_id] = {
                "id": message_id,
                "thread_id": f"thread-{index + 1}",
                "from": f"{name} <sales@{domain}>",
                "to": "manager@example.invalid",
                "subject": f"{name} — đề nghị gặp tuần sau",
                "date": self.now.isoformat(),
                "body": f"Chào anh/chị, chúng tôi là {name}. Website: https://{domain}/. Mong trao đổi về sản phẩm vào {start.isoformat()} trong 30 phút. Đây là email và công ty giả lập.",
                "company_name": name,
                "meeting": {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                },
                "rfc_message_id": f"<{message_id}@{domain}>",
                "attachments": [],
                "body_truncated": False,
                "demo": True,
            }
        duplicate = copy.deepcopy(self.messages["demo-1"])
        duplicate.update(
            id="demo-4",
            subject="Atlas Labs — bổ sung nội dung trao đổi",
            body="Chúng tôi gửi bổ sung thông tin sản phẩm tại https://atlas.example.test/. Dữ liệu mô phỏng.",
        )
        duplicate.pop("meeting")
        self.messages["demo-4"] = duplicate

    def _trashed(self, message_id):
        if self.store:
            try:
                return self.store.get("demo_mail", self.owner, message_id).get(
                    "trashed", False
                )
            except ResearchError:
                pass
        return False

    def list_mail(self, query="in:inbox", limit=20):
        values = [
            dict(value, snippet=value["body"][:180])
            for value in self.messages.values()
            if not self._trashed(value["id"])
        ]
        values = [
            {key: val for key, val in value.items() if key not in {"body", "meeting"}}
            for value in values
        ]
        return {"messages": values[:limit], "next_page_token": "", "errors": []}

    def read_mail(self, message_id):
        if message_id not in self.messages:
            raise ResearchError("NOT_FOUND", "Không tìm thấy email demo.", 404)
        return copy.deepcopy(self.messages[message_id])

    def busy(self, start, end, timezone_name):
        meeting = self.monday.replace(hour=10)
        intervals = [
            (meeting, meeting + timedelta(hours=1, minutes=30)),
            (
                meeting + timedelta(days=1, hours=4),
                meeting + timedelta(days=1, hours=5),
            ),
        ]
        return [(a, b) for a, b in intervals if a < end and b > start]

    def events(self, start, end, timezone_name):
        meeting = self.monday.replace(hour=11)
        return (
            [
                {
                    "id": "demo-existing",
                    "start": {"dateTime": meeting.isoformat()},
                    "end": {"dateTime": (meeting + timedelta(minutes=30)).isoformat()},
                    "attendees": [{"email": "sales@meridian.example.test"}],
                }
            ]
            if meeting < end and meeting + timedelta(minutes=30) > start
            else []
        )

    def _profile(self, domain):
        return next(
            (value for value in self.COMPANIES if value[0] == domain),
            (
                domain,
                domain,
                "Chưa có dữ liệu",
                "Nội dung mô phỏng",
                "Chưa có dữ liệu",
                "Chưa công bố",
            ),
        )

    def _page(self, url, topic="official"):
        domain = domain_of(url)
        company_domain = next(
            (
                value[0]
                for value in self.COMPANIES
                if domain == value[0] or f"/{value[0]}/" in url
            ),
            domain,
        )
        _, name, industry, products, scale, pricing = self._profile(company_domain)
        details = [
            ("overview", f"{name} là doanh nghiệp giả lập dùng để minh họa báo cáo."),
            ("industry", industry),
            ("products", products),
            ("scale", scale),
            ("contacts", f"sales@{company_domain}"),
            ("pricing", pricing),
            ("people", f"Người phụ trách demo của {name}; hồ sơ nghề nghiệp giả lập."),
        ]
        if topic == "news":
            details = [
                (
                    "news",
                    f"Bản tin giả lập: {name} công bố chương trình hợp tác sản phẩm mới.",
                )
            ]
        if topic == "risk":
            details = [
                (
                    "risk",
                    f"Tình huống giả lập: có phản ánh chưa được xác minh về chậm bàn giao tại {name}; chưa có kết luận chính thức.",
                )
            ]
        person = {
            "atlas.example.test": "Lê An",
            "meridian.example.test": "Trần Minh",
            "northstar.example.test": "Đỗ Hà",
        }.get(company_domain, "Người phụ trách demo")
        people = {}
        if topic == "official":
            details = [item for item in details if item[0] != "people"]
            for section, statement in [
                (
                    "career",
                    f"{person} là người phụ trách doanh nghiệp {name} trong dữ liệu mô phỏng; đây là nhân vật giả lập.",
                ),
                (
                    "relationships",
                    f"Trong kịch bản giả lập, {person} phụ trách hợp tác sản phẩm giữa {name} và đối tác mô phỏng Example Partner.",
                ),
                (
                    "profiles",
                    f"Hồ sơ nghề nghiệp demo của {person} được minh họa tại https://{company_domain}/team/; không phải hồ sơ người thật.",
                ),
            ]:
                details.append(("people", statement))
                people[statement] = {"person": person, "person_section": section}
        text = "\n".join(value for _, value in details)
        return {
            "id": "src-" + digest(url)[:20],
            "url": url,
            "title": f"{name} — {topic} (DEMO)",
            "kind": "official" if topic == "official" else "news",
            "retrieved_at": utcnow(),
            "published_at": (self.now - timedelta(days=3)).isoformat()
            if topic != "official"
            else None,
            "text": text,
            "access": "fixture",
            "demo": True,
            "links": [],
            "fixture_facts": details,
            "fixture_people": people,
        }

    def crawl(self, website, limit, deadline):
        return [self._page(normalize_url(website))], []

    def search(self, name, topic, deadline):
        company = next((item for item in self.COMPANIES if item[1] == name), None)
        if company is None:
            raise ResearchError(
                "DEMO_SOURCE_UNAVAILABLE",
                "Công ty này chưa có fixture tìm kiếm; không dùng nguồn của công ty khác.",
            )
        kind = "risk" if "risk" in topic else "news"
        return [
            {
                "url": f"https://news.example.test/{company[0]}/{kind}",
                "title": f"{name} — {kind} (DEMO)",
                "snippet": "Nguồn giả lập, không phải tin tức thực.",
                "demo": True,
            }
        ]

    def fetch(self, url, deadline):
        return self._page(url, "risk" if url.endswith("/risk") else "news")

    def check_write(self, kind):
        return None

    def send(self, payload):
        return {
            "message_id": "demo-send-" + str(uuid.uuid4()),
            "status": "simulated_send",
            "delivery_confirmed": False,
            "demo": True,
        }

    def delete_event(self, payload):
        """Simulated deletion; the real calendar is never touched."""
        return {"event_id": payload["event_id"], "status": "simulated_delete", "demo": True}

    def create_event(self, payload):
        """Simulated event creation; the real calendar is never touched."""
        return {
            "event_id": "demo-event-" + str(uuid.uuid4()),
            "status": "simulated_create",
            "demo": True,
        }

    def trash(self, payload):
        already = self._trashed(payload["message_id"])
        if self.store:
            self.store.put(
                "demo_mail", self.owner, payload["message_id"], {"trashed": True}
            )
        return {
            "message_id": payload["message_id"],
            "status": "already_in_trash" if already else "simulated_trash",
            "demo": True,
        }


class CustomerSampleBackend(DemoBackend):
    """The user's six sample inputs; no invented research about real companies."""

    dataset = "customer"
    input_only = True

    def __init__(self, settings, owner="", store=None, now=None):
        super().__init__(settings, owner, store, now)
        fixture = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "fixtures"
                / "customer_emails.json"
            ).read_text(encoding="utf-8")
        )
        self.samples = fixture["emails"]
        self.messages = {}
        for sample in self.samples:
            placeholder = sample["id"] + "@example.invalid"
            self.messages[sample["id"]] = {
                "id": sample["id"],
                "thread_id": "thread-" + sample["id"],
                "from": f"{sample['sender_name']} — {sample['company']} <{placeholder}>",
                "to": "manager@example.invalid",
                "subject": sample["company"] + " — email mẫu",
                "subject_generated": True,
                "date": "",
                "body": sample["content"] + "\nWebsite: " + sample["website"],
                "sample_content": sample["content"],
                "sample_sender_name": sample["sender_name"],
                "company_name": sample["company"],
                "provided_website": sample["website"],
                "rfc_message_id": "<" + placeholder + ">",
                "attachments": [],
                "body_truncated": False,
                "demo": True,
                "sender_address_placeholder": True,
                "meeting_clarification": sample.get("meeting_clarification", ""),
                "attachment_notice": sample.get("attachment_notice", ""),
            }

    def crawl(self, website, limit, deadline):
        domain = domain_of(website)
        sample = next(
            (item for item in self.samples if domain_of(item["website"]) == domain),
            None,
        )
        if sample is None:
            return [], [
                {
                    "code": "SAMPLE_NOT_FOUND",
                    "message": "Không có email mẫu khớp công ty đã chọn.",
                }
            ]
        details = [
            ("overview", "Nội dung email mẫu: " + sample["content"]),
            (
                "contacts",
                f"Người gửi trong email mẫu: {sample['sender_name']} — {sample['company']}",
            ),
            ("overview", "Website trong email mẫu: " + sample["website"]),
        ]
        source = {
            "id": "input-" + digest(sample["id"])[:20],
            "url": "",
            "provided_website": sample["website"],
            "title": "Email mẫu do người dùng cung cấp — " + sample["company"],
            "kind": "sample_email",
            "retrieved_at": utcnow(),
            "published_at": None,
            "text": "\n".join(value for _, value in details),
            "access": "user_provided",
            "demo": True,
            "links": [],
            "fixture_facts": details,
            "fixture_origin": "user_sample",
        }
        return [source], [
            {
                "code": "INPUT_ONLY",
                "message": "Chỉ có dữ liệu email mẫu do bạn cung cấp; chưa đọc website, tin tức hoặc xác minh hồ sơ công ty trong bộ mẫu này.",
            }
        ]

    def search(self, name, topic, deadline):
        raise ResearchError(
            "SAMPLE_RESEARCH_UNAVAILABLE",
            "Bộ email mẫu không chứa kết quả tìm kiếm Internet; không tạo tin tức hoặc thông tin bất lợi giả cho công ty thật.",
        )

    def fetch(self, url, deadline):
        raise ResearchError(
            "SAMPLE_RESEARCH_UNAVAILABLE",
            "Chưa có nội dung website được thu thập cho bộ email mẫu.",
        )

    def events(self, start, end, timezone_name):
        return []
