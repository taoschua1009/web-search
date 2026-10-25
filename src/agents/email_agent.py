"""Email role: reads messages and resolves the company behind each one."""

from __future__ import annotations

import re
from email.utils import parseaddr

from src.schemas.errors import ResearchError, error_view
from src.schemas.research import Meeting
from src.services.research.domains import FREE_EMAIL_DOMAINS, NON_COMPANY_DOMAINS
from src.services.research.web_fetcher import domain_of, normalize_url

MEETING_WORDS = re.compile(
    r"\b(meeting|meet|call|schedule|appointment|demo)\b|họp|gặp|cuộc hẹn|lịch hẹn",
    re.IGNORECASE,
)


class EmailAgent:
    """Reads e-mails and works out the company behind each and whether a meeting is requested."""

    name = "email"

    def __init__(self, backend, reasoner):
        self.backend, self.reasoner = backend, reasoner
        # Set by resolve(): does this email ask for a meeting? Only then is
        # the calendar checked.
        self.meeting_requested = False

    def read(self, ids):
        """Read each message once; unreadable messages become errors."""
        messages, errors = [], []
        for message_id in dict.fromkeys(ids):
            try:
                messages.append(self.backend.read_mail(message_id))
            except ResearchError as error:
                errors.append(dict(error_view(error), message_id=message_id))
        return messages, errors

    def resolve(self, message, selection, now, request, deadline, extract_context=True):
        """Return (company, meeting, clarification, errors) and set ``meeting_requested``."""
        context = None
        errors = []
        if self.backend.mode == "live" and extract_context:
            try:
                context = self.reasoner.email_context(
                    message, now, request.timezone, request.duration_minutes, deadline
                )
            except ResearchError as error:
                errors.append(error_view(error))
        name = message.get("company_name") or (context.company_name if context else "")
        meeting = request.meeting
        if not meeting:
            if self.backend.mode == "demo" and message.get("meeting"):
                meeting = Meeting.model_validate(message["meeting"])
            elif context:
                meeting = context.meeting
        clarification = message.get("meeting_clarification") or (
            context.clarification if context else ""
        )
        self.meeting_requested = bool(
            meeting
            or clarification
            or (
                context.meeting_requested
                if context
                else self.backend.mode == "live"
                and MEETING_WORDS.search(
                    message.get("subject", "") + "\n" + message.get("body", "")[:3000]
                )
            )
        )
        if selection:
            return (
                {
                    "name": selection.name.strip(),
                    "domain": domain_of(selection.website),
                    "website": normalize_url(selection.website),
                    "evidence": "user_selection",
                },
                meeting,
                clarification,
                errors,
            )
        sender = parseaddr(message.get("from", ""))[1]
        sender_domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
        urls = []
        for candidate in re.findall(r'https?://[^\s<>"\)]+', message.get("body", "")):
            try:
                url = normalize_url(candidate.rstrip(".,;"))
                host = domain_of(url)
                unrelated = any(
                    host == value or host.endswith("." + value)
                    for value in NON_COMPANY_DOMAINS
                )
                action_link = re.search(
                    r"unsubscribe|opt[-_]?out|logout|reset[-_]?password|[?&](?:token|code|auth)=",
                    url,
                    re.IGNORECASE,
                )
                if not unrelated and not action_link:
                    urls.append(url)
            except ResearchError:
                continue
        by_domain = {}
        for url in sorted(urls, key=len):
            by_domain.setdefault(domain_of(url), url)
        if (
            sender_domain
            and sender_domain not in FREE_EMAIL_DOMAINS | NON_COMPANY_DOMAINS
        ):
            matching = [
                host
                for host in by_domain
                if host == sender_domain or sender_domain.endswith("." + host)
            ]
            if matching:
                domain = matching[0]
                return (
                    {
                        "name": name or domain,
                        "domain": domain,
                        "website": by_domain[domain],
                        "evidence": "email_domain_and_url",
                    },
                    meeting,
                    clarification,
                    errors,
                )
            if not by_domain:
                domain = domain_of("https://" + sender_domain)
                return (
                    {
                        "name": name or domain,
                        "domain": domain,
                        "website": "https://" + domain + "/",
                        "evidence": "sender_domain_candidate",
                    },
                    meeting,
                    clarification,
                    errors,
                )
        if len(by_domain) == 1:
            domain, website = next(iter(by_domain.items()))
            return (
                {
                    "name": name or domain,
                    "domain": domain,
                    "website": website,
                    "evidence": "email_url_candidate",
                },
                meeting,
                clarification,
                errors,
            )
        return (
            {
                "name": name,
                "domain": "",
                "website": "",
                "evidence": "ambiguous",
                "candidates": list(by_domain.values())[:5],
            },
            meeting,
            clarification,
            errors,
        )
