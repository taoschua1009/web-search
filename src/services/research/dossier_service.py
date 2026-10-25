"""Validation, shaping and scoring of company dossiers."""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime

from src.schemas.dossier import Dossier
from src.schemas.errors import ResearchError
from src.schemas.research import Fact
from src.services.research.domains import SOCIAL_HOSTS
from src.services.research.utils import digest, utcnow
from src.services.research.web_fetcher import domain_of, normalize_url, url_key


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").casefold().strip()


def _clip(value: str, size: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= size else value[: size - 1].rstrip() + "…"


def _parse_date(value: str):
    try:
        parsed = datetime.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


class RetrievalState:
    """Pages and search hits retrieved during one research run, keyed by URL."""

    def __init__(self):
        self.lock = threading.Lock()
        self.searches = self.fetches = self.recent_searches = 0
        self.rejected_shallow = False
        self.pages: dict = {}  # url_key -> fetched source
        self.hits: dict = {}  # url_key -> search hit
        self.dossier: Dossier | None = None


def hit_source_id(url):
    """Id of a search-result source built from its snippet."""
    return "src-" + digest(url)[:20]


class _Resolver:
    """Map cited URLs onto retrieved sources; snippets become snippet-only sources."""

    def __init__(self, state):
        self.state, self.used, self.dropped = state, {}, 0

    def source_ids(self, urls):
        ids = []
        for url in urls[:8]:
            try:
                key = url_key(url)
            except ResearchError:
                continue
            source = self.state.pages.get(key)
            if source is None:
                hit = self.state.hits.get(key)
                if hit is None:
                    continue
                source = {
                    "id": hit_source_id(hit["url"]),
                    "url": hit["url"],
                    "title": hit.get("title") or hit["url"],
                    "kind": "search_result",
                    "retrieved_at": utcnow(),
                    "published_at": None,
                    "text": hit.get("snippet", ""),
                    "access": "snippet_only",
                    "demo": False,
                }
            self.used[source["id"]] = source
            if source["id"] not in ids:
                ids.append(source["id"])
        return ids

    def text_of(self, ids):
        return " ".join(_norm(self.used[i].get("text", "")) for i in ids)

    def cited(self, item, text_limit=700):
        ids = self.source_ids(item.urls)
        if not ids:
            self.dropped += 1
            return None
        return ids

    def cited_text(self, item):
        ids = self.cited(item)
        if ids is None:
            return None
        evidence = _norm(item.evidence)
        verified = (evidence in self.text_of(ids)) if evidence else None
        return {
            "text": _clip(item.text, 700),
            "source_ids": ids,
            "estimated": item.estimated,
            "verified": verified,
            "evidence": _clip(item.evidence, 300) if verified else "",
        }


def finalize(raw: Dossier, state, company):
    """Return (dossier dict with source_ids, used sources, dropped item count)."""
    r = _Resolver(state)

    def many(items, build):
        out = []
        for item in items:
            built = build(item)
            if built is not None:
                out.append(built)
        return out

    def with_ids(item, **fields):
        ids = r.cited(item)
        return None if ids is None else dict(fields, source_ids=ids)

    headline = raw.headline
    website = ""
    try:
        website = normalize_url(headline.website) if headline.website else ""
    except ResearchError:
        website = ""
    official_domain = domain_of(website) if website else company.get("domain", "")
    news_ids, news_dates = set(), {}

    def build_news(item):
        built = with_ids(item, text=_clip(item.text, 500), date=item.date[:10])
        if built:
            news_ids.update(built["source_ids"])
            if _parse_date(item.date):
                for source_id in built["source_ids"]:
                    news_dates.setdefault(source_id, item.date[:10])
        return built

    def build_profile(item):
        ids = r.source_ids(item.urls)
        link = ""
        if item.link:
            try:
                link = normalize_url(item.link)
                if url_key(link) not in state.pages and url_key(link) not in state.hits:
                    link = ""
            except ResearchError:
                link = ""
        if not ids:
            r.dropped += 1
            return None
        return {
            "platform": _clip(item.platform, 60),
            "handle": _clip(item.handle, 120),
            "detail": _clip(item.detail, 300),
            "link": link,
            "source_ids": ids,
        }

    def build_founder(person):
        return {
            "name": _clip(person.name, 120),
            "role": _clip(person.role, 160),
            "subtitle": _clip(person.subtitle, 200),
            "achievements": many(person.achievements, r.cited_text),
            "network": many(
                person.network,
                lambda x: with_ids(
                    x, name=_clip(x.name, 160), detail=_clip(x.detail, 400)
                ),
            ),
            "profiles": many(person.profiles, build_profile),
            "style": r.cited_text(person.style) if person.style else None,
            "strength": r.cited_text(person.strength) if person.strength else None,
        }

    def build_product(item):
        ids = r.source_ids(item.urls)
        if not ids:
            r.dropped += 1
            return None
        return {
            "name": _clip(item.name, 160),
            "tag": _clip(item.tag, 60),
            "price_visibility": item.price_visibility,
            "summary": _clip(item.summary, 400),
            "bullets": [_clip(b, 300) for b in item.bullets[:8]],
            "prices": [
                {
                    "label": _clip(p.label, 80),
                    "value": _clip(p.value, 80),
                    "note": _clip(p.note, 200),
                }
                for p in item.prices[:4]
            ],
            "callout": _clip(item.callout, 300),
            "source_ids": ids,
        }

    team = raw.team
    dossier = {
        "mode": "agent",
        "headline": {
            "name": _clip(headline.name or company.get("name", ""), 160),
            "tagline": _clip(headline.tagline, 200),
            "status": _clip(headline.status, 60),
            "hq": _clip(headline.hq, 120),
            "industry": _clip(headline.industry, 120),
            "size_label": _clip(headline.size_label, 80),
            "website": website,
            "identity_note": _clip(headline.identity_note, 500),
        },
        "summary": r.cited_text(raw.summary) if raw.summary else None,
        "kpis": many(
            raw.kpis[:6],
            lambda x: with_ids(
                x,
                value=_clip(x.value, 24),
                unit=_clip(x.unit, 24),
                label=_clip(x.label, 120),
                estimated=x.estimated,
            ),
        ),
        "profile": many(
            raw.profile[:16],
            lambda x: with_ids(x, label=_clip(x.label, 60), value=_clip(x.value, 300)),
        ),
        "team": {
            "size": r.cited_text(team.size) if team.size else None,
            "signals": many(team.signals[:6], r.cited_text),
            "culture": r.cited_text(team.culture) if team.culture else None,
            "openings": many(
                team.openings[:14],
                lambda x: with_ids(
                    x, title=_clip(x.title, 120), kind=_clip(x.kind, 60)
                ),
            ),
            "hiring_note": r.cited_text(team.hiring_note) if team.hiring_note else None,
        },
        "founders": [build_founder(person) for person in raw.founders[:4]],
        "products": many(raw.products[:8], build_product),
        "customers_partners": many(raw.customers_partners[:10], r.cited_text),
        "financials": many(raw.financials[:8], r.cited_text),
        "news": many(raw.news[:8], build_news),
        "risks": many(
            raw.risks[:8],
            lambda x: with_ids(x, text=_clip(x.text, 600), status=x.status),
        ),
        "risk_note": _clip(raw.risk_note, 600),
        "meeting_prep": [_clip(x, 300) for x in raw.meeting_prep[:6] if x.strip()],
        "conflicts": [_clip(x, 400) for x in raw.conflicts[:6] if x.strip()],
        "gaps": [_clip(x, 300) for x in raw.gaps[:10] if x.strip()],
    }
    sources = []
    for source in r.used.values():
        source = dict(source)
        host = domain_of(source["url"])
        if official_domain and (
            host == official_domain or host.endswith("." + official_domain)
        ):
            source["kind"] = "official"
        elif any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
            source["kind"] = "social"
        elif source["id"] in news_ids:
            source["kind"] = "news"
        if not source.get("published_at") and source["id"] in news_dates:
            source["published_at"] = news_dates[source["id"]]
        sources.append(source)
    return dossier, sources, r.dropped


CATEGORY_LABELS = {
    "overview": "Tổng quan",
    "industry": "Lĩnh vực",
    "contacts": "Liên hệ",
}


def dossier_from_facts(company, facts):
    """Deterministic dossier from cited facts (demo mode or when the agent fails)."""

    def by(*categories):
        return [f for f in facts if f["category"] in categories]

    def cited(fact):
        return {
            "text": _clip(fact["text"], 700),
            "source_ids": list(fact["source_ids"]),
            "estimated": False,
            "verified": True if fact.get("evidence") else None,
        }

    overview = by("overview")
    people = {}
    for fact in by("people"):
        person = fact.get("person") or "Người chủ chốt"
        entry = people.setdefault(
            person,
            {
                "name": person,
                "role": "",
                "subtitle": "",
                "achievements": [],
                "network": [],
                "profiles": [],
                "style": None,
                "strength": None,
            },
        )
        section = fact.get("person_section", "career")
        if section == "career":
            entry["achievements"].append(cited(fact))
        elif section == "relationships":
            entry["network"].append(
                {
                    "name": "",
                    "detail": _clip(fact["text"], 400),
                    "source_ids": list(fact["source_ids"]),
                }
            )
        else:
            entry["profiles"].append(
                {
                    "platform": "Hồ sơ công khai",
                    "handle": "",
                    "detail": _clip(fact["text"], 300),
                    "link": "",
                    "source_ids": list(fact["source_ids"]),
                }
            )
    products = [
        {
            "name": "Sản phẩm & dịch vụ" if f["category"] == "products" else "Giá bán",
            "tag": "",
            "price_visibility": "public" if f["category"] == "pricing" else "unknown",
            "summary": _clip(f["text"], 400),
            "bullets": [],
            "prices": [],
            "callout": "",
            "source_ids": list(f["source_ids"]),
        }
        for f in by("products", "pricing")[:8]
    ]
    missing = [
        label
        for label, categories in (
            ("Người sáng lập / lãnh đạo", ("people",)),
            ("Sản phẩm & giá bán", ("products", "pricing")),
            ("Quy mô & tài chính", ("scale", "financials")),
            ("Khách hàng & đối tác", ("partners",)),
            ("Tin tức 30 ngày", ("news",)),
        )
        if not by(*categories)
    ]
    return {
        "mode": "facts",
        "headline": {
            "name": company["name"],
            "tagline": "",
            "status": "",
            "hq": "",
            "industry": "",
            "size_label": "",
            "website": company.get("website", ""),
            "identity_note": "",
        },
        "summary": cited(overview[0]) if overview else None,
        "kpis": [],
        "profile": [
            {
                "label": CATEGORY_LABELS.get(f["category"], f["category"]),
                "value": _clip(f["text"], 300),
                "source_ids": list(f["source_ids"]),
            }
            for f in by("industry", "contacts", "overview")[:8]
        ],
        "team": {
            "size": None,
            "signals": [cited(f) for f in by("scale")[:6]],
            "culture": None,
            "openings": [],
            "hiring_note": None,
        },
        "founders": list(people.values())[:4],
        "products": products,
        "customers_partners": [cited(f) for f in by("partners")[:10]],
        "financials": [cited(f) for f in by("financials")[:8]],
        "news": [
            {
                "text": _clip(f["text"], 500),
                "date": "",
                "source_ids": list(f["source_ids"]),
            }
            for f in by("news")[:8]
        ],
        "risks": [
            {
                "text": _clip(f["text"], 600),
                "status": f.get("status")
                if f.get("status")
                in {"allegation", "investigation", "concluded_finding"}
                else "source_statement",
                "source_ids": list(f["source_ids"]),
            }
            for f in by("risk")[:8]
        ],
        "risk_note": "",
        "meeting_prep": [],
        "conflicts": [],
        "gaps": missing,
    }


def dossier_source_ids(dossier):
    """Every source id referenced anywhere in a dossier."""
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "source_ids":
                    found.update(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(dossier)
    return found


CONTACT_WORDS = re.compile(
    r"email|mail|điện thoại|hotline|zalo|địa chỉ|trụ sở|liên hệ|phone|address|contact",
    re.IGNORECASE,
)


def scorecard(dossier, sources, elapsed):
    """Quality rows (criterion, result) computed only from what the dossier holds."""
    head = dossier["headline"]
    team = dossier["team"]
    checks = [
        ("Website", bool(head["website"])),
        ("Lĩnh vực", bool(head["industry"] or dossier["profile"])),
        ("Sản phẩm", bool(dossier["products"])),
        ("Quy mô", bool(dossier["kpis"] or team["size"] or team["signals"])),
        ("Tin tức", bool(dossier["news"])),
        (
            "Liên hệ",
            any(CONTACT_WORDS.search(x["label"]) for x in dossier["profile"]),
        ),
        ("Người sáng lập", bool(dossier["founders"])),
        (
            "Giá bán / đối tác",
            bool(
                dossier["customers_partners"]
                or any(p["prices"] for p in dossier["products"])
            ),
        ),
    ]
    present = [name for name, ok in checks if ok]
    missing = [name for name, ok in checks if not ok]
    percent = round(100 * len(present) / len(checks))

    cited_items = []

    def collect(value):
        if isinstance(value, dict):
            if "verified" in value and "text" in value:
                cited_items.append(value)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(dossier)
    quoted = [x for x in cited_items if x["verified"] is not None]
    verified = sum(1 for x in quoted if x["verified"])
    accuracy = (
        f"{verified}/{len(quoted)} nhận định có trích đoạn khớp nguyên văn nguồn"
        if quoted
        else "Chưa có trích đoạn đối chiếu nguyên văn"
    )
    conflicts = dossier["conflicts"]
    accuracy += (
        f"; {len(conflicts)} số liệu mâu thuẫn giữa các nguồn (liệt kê bên dưới)"
        if conflicts
        else "; không phát hiện mâu thuẫn giữa các nguồn đã đọc"
    )

    kinds = {}
    for source in sources:
        label = {
            "official": "Website chính thức",
            "social": "Mạng xã hội",
            "news": "Báo chí",
        }.get(source.get("kind"), "Nguồn web khác")
        kinds[label] = kinds.get(label, 0) + 1
    breakdown = ", ".join(f"{label}: {count}" for label, count in kinds.items())

    cutoff = datetime.now(UTC).timestamp() - 30 * 86400
    recent = [
        item
        for item in dossier["news"]
        if (parsed := _parse_date(item["date"])) and parsed.timestamp() >= cutoff
    ]
    ready = percent >= 75 and len(sources) >= 5
    return [
        {
            "criterion": "Độ đầy đủ thông tin",
            "result": f"{percent}% ({', '.join(present) or 'chưa có mục nào'})"
            + (f" · thiếu: {', '.join(missing)}" if missing else ""),
        },
        {"criterion": "Độ chính xác", "result": accuracy},
        {"criterion": "Thời gian tạo báo cáo", "result": f"{round(elapsed)} giây"},
        {
            "criterion": "Số nguồn tham khảo",
            "result": f"{len(sources)} nguồn"
            + (f" ({breakdown})" if breakdown else ""),
        },
        {
            "criterion": "Độ mới của dữ liệu",
            "result": f"{len(recent)} tin có ngày trong 30 ngày gần nhất"
            if recent
            else "Chưa xác nhận được tin có ngày trong 30 ngày gần nhất",
        },
        {
            "criterion": "Mức sẵn sàng",
            "result": "Đủ thông tin để chuẩn bị cuộc họp"
            if ready
            else "Cần bổ sung trước cuộc họp: "
            + (", ".join(missing) or "thêm nguồn đối chiếu"),
        },
    ], percent


def dossier_digest(dossier):
    """Compact view for the chat model; the full dossier lives on the report page."""
    if not dossier:
        return None
    head = dossier["headline"]
    return {
        "name": head["name"],
        "website": head["website"],
        "industry": head["industry"],
        "hq": head["hq"],
        "identity_note": head["identity_note"],
        "summary": (dossier["summary"] or {}).get("text", ""),
        "kpis": [
            f"{k['value']} {k['unit']} {k['label']}".replace("  ", " ").strip()
            for k in dossier["kpis"]
        ],
        "founders": [
            {"name": f["name"], "role": f["role"]} for f in dossier["founders"]
        ],
        "products": [p["name"] for p in dossier["products"]],
        "news": [n["text"] for n in dossier["news"][:3]],
        "risks": [r["text"] for r in dossier["risks"][:3]],
        "conflicts": dossier["conflicts"],
        "gaps": dossier["gaps"],
        "meeting_prep": dossier["meeting_prep"],
    }


def exact_span(evidence, text):
    """The original substring of ``text`` that matches ``evidence`` ignoring case/spacing."""
    words = re.findall(r"\S+", evidence or "")
    if not words:
        return ""
    match = re.search(
        r"\s+".join(re.escape(word) for word in words), text or "", re.IGNORECASE
    )
    return match.group(0) if match else ""


def facts_from_dossier(dossier, sources):
    """Knowledge facts for items whose quoted evidence matches the cited page text."""
    by_id = {source["id"]: source for source in sources}
    facts = []

    def add(category, item, **extra):
        if not item or item.get("verified") is not True:
            return
        for source_id in item["source_ids"]:
            span = exact_span(
                item.get("evidence", ""), by_id[source_id].get("text", "")
            )
            if span:
                facts.append(
                    Fact(
                        id="fact-" + digest([source_id, category, span])[:20],
                        category=category,
                        text=item["text"],
                        evidence=span[:1500],
                        source_ids=[source_id],
                        **extra,
                    ).model_dump()
                )
                return

    add("overview", dossier.get("summary"))
    team = dossier["team"]
    for item in [team["size"], team["culture"], team["hiring_note"], *team["signals"]]:
        add("scale", item)
    for person in dossier["founders"]:
        for item in person["achievements"]:
            add("people", item, person=person["name"], person_section="career")
    for item in dossier["customers_partners"]:
        add("partners", item)
    for item in dossier["financials"]:
        add("financials", item)
    return facts
