"""Host and domain lists the research agents use to tell companies from everything else."""

from __future__ import annotations

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "icloud.com",
    "proton.me",
    "protonmail.com",
    "live.com",
}


NON_COMPANY_DOMAINS = {
    "facebook.com",
    "linkedin.com",
    "google.com",
    "youtube.com",
    "youtu.be",
    "wikipedia.org",
    "calendly.com",
    "zoom.us",
    "meet.google.com",
    "t.co",
}


SOCIAL_HOSTS = ("facebook.com", "linkedin.com", "github.com", "huggingface.co")


NEUTRAL_HOSTS = (
    "facebook.com",
    "linkedin.com",
    "github.com",
    "huggingface.co",
    "youtube.com",
    "wikipedia.org",
    "tracxn.com",
    "crunchbase.com",
    "itviec.com",
    "topcv.vn",
    "glassdoor.com",
    "twitter.com",
    "x.com",
    "reddit.com",
)
