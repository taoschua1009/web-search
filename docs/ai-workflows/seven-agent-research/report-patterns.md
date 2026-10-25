# Report Presentation References

- Version: 0.1, 2026-10-25; companion to feature specification 0.5 (presentation content unchanged).
- Status: company/person presentations implemented in the research workspace; see `implementation.md` for verification and remaining extensions.
- Scope: generate company reports for arbitrary companies discovered in selected emails. FPT and ProtonX are examples, never hardcoded targets.

## Reference Provenance

The user supplied two images on 2026-10-25:

1. `Screenshot 2026-10-25 at 8.09.53 PM.png`: company dashboard, headline metrics, company/team details, and products/prices.
2. `Screenshot 2026-10-25 at 8.10.24 PM.png`: founder section with achievements, professional relationships, and digital profiles.

Original files are on the user's Desktop; their filenames use a narrow nonbreaking space before PM. The images are layout and content examples. No displayed business claim, personal achievement, relationship, follower count, price, or staffing estimate has been independently verified in this task. Do not seed production company knowledge from screenshot text.

## Company List Before the Report

A selected mailbox scope can yield many companies. Show one entry per resolved company, including name/domain, relevant email count, latest message date, research status, last report update, and report link. Include unresolved associations and partial failures. A company with no completed report shows its actual state rather than another company's content.

The list is a proposed navigation aid. It does not authorize scanning the whole mailbox beyond the user's requested scope.

## Presentation A: Company Profile and Dashboard

| Section | Content | Evidence and missing-data handling |
|---|---|---|
| Header | Company name, website, industry, location, reported status, report timestamp | Resolve the correct entity. Cite business status claims; an accessible website alone does not establish active legal status. |
| Quick overview | A few relevant KPI cards and a short meeting-preparation summary | Select metrics appropriate to the company's industry. Cite every figure and its date, unit, and exact/estimated status. |
| Company profile | Business description, public address/contact channels, business positioning | Preserve source attribution. Unavailable fields display “Not found” or remain explicitly absent. |
| Team and hiring | Supported headcount/range, leadership, and current public vacancies | Keep inferred estimates labeled. Repository members or social followers are not employee counts; a job advertisement does not prove a position has no current staff. |
| Products/services and pricing | Offerings, use cases, features, public prices or quote-required status | Currency, billing basis, and price date accompany a figure. No published price means “Not publicly available,” not zero. Vendor performance claims remain attributed claims. |
| Key people | Founder, leadership, or meeting-relevant contact summaries with detail links | Include only resolved professional identities and sourced affiliations. A founder is not always the email sender or meeting attendee. |
| News and risks | Recent developments and supported adverse findings | Dates, source type, status, uncertainty, follow-up, and response follow the main spec's evidence standard. |
| Meeting preparation | Upcoming proposal, next-week free/busy windows, suggested questions, reply draft | Show timezone and checked-at time. Suggested questions are recommendations, not factual claims or calendar reservations. |
| Sources and quality | Claim citations, source list, completeness/freshness/latency measurements | Use actual run metadata. Show gaps and conflicting evidence; do not copy sample scorecard values. |

The sample's AI/OCR/open-source metrics are examples of company-specific content. A manufacturer, bank, or retailer uses relevant sourced products/metrics instead. Non-applicable fields must not reduce coverage unfairly or trigger invented substitute data.

## Presentation B: Founder or Key-Person Profile

| Section | Content | Evidence and missing-data handling |
|---|---|---|
| Identity header | Resolved name, professional role, associated company and applicable dates | Check identity using company affiliation and profile evidence; matching names alone are insufficient. |
| Career and achievements | Relevant professional history, publicly documented qualifications/awards | Each substantive claim links to evidence and dates. An unsupported “first,” “leading,” or award claim is omitted or clearly attributed. |
| Professional relationships | Publicly documented advisors, collaborations, investments, or professional connections | Describe the specific evidenced relationship. A shared event, photo, following, or mention does not establish a partnership, advisory role, or endorsement. |
| Digital profiles | Verified-to-identity professional profile links, relevant public work/repositories, dated public metrics | Distinguish access limitations. Followers, downloads, and repository stars do not establish revenue, expertise, or influence on their own. |
| Meeting context | Public work topics and relevant questions for the planned conversation | Base suggestions on cited professional activity; avoid unsupported personality or behavioral judgments. |

If founder/key-person evidence is unavailable or identity is uncertain, show a clear missing/needs-confirmation state. Do not force a founder biography into every report. The person view may be embedded in the company report or opened as an owned linked detail; navigation is not yet fixed.

## Visual Direction and Interaction

Use the screenshots as guidance for a dark background, contrasting cards, section numbering, compact status badges, metric cards, short summaries, and clear content hierarchy. These are presentation references, not a requirement to replicate every pixel or placeholder value.

Proposed responsive behavior: a multi-column card grid on desktop collapses to one readable column on mobile. Keep adequate text contrast, accessible controls, visible keyboard focus, and usable source links. Charts appear only when actual comparable data and units exist; decorative progress bars must not imply a measured score without a defined scale.

Place source links beside the claims or metrics they support. Show source date and uncertainty without hiding evidence behind an unexplained global credibility badge. Knowledge edits and proposed sends/Trash actions use explicit controls tied to the current company and version; external article text cannot create an approval control.

A report is allowed to be shorter than the examples when evidence is sparse. Available information, missing inputs, source failures, and stale evidence remain visible.
