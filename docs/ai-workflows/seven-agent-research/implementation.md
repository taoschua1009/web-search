# Seven-Agent Research: Implementation and Verification

Updated: 2026-10-25. Implements the confirmed core of specification 0.4 after the user's instruction to implement it. The reference-table extensions remain proposed, as recorded in the specification.

## Delivered behavior

- An authenticated research workspace at `/research`, with bounded email selection, company identity resolution, separate company runs/reports, progress, and per-company failure states.
- Seven explicit roles, one module each under `src/agents/` (`email_agent.py`, `web_research_agent.py`, `company_info_agent.py`, `calendar_agent.py`, `report_agent.py`, `memory_agent.py`, `approval_agent.py`): Email, Web Research, Company Info, Calendar, Report Generation, Memory, and Human Approval. A deterministic coordinator calls these roles; model extraction is bounded and receives no action tools.
- Public website retrieval, user-selected Tavily search or optional configured Google search, cited company facts, key-person sections where supported, dated news/adverse information, and transparent gaps. Literal evidence is validated against a known source before a fact is rendered. A single-source adverse finding is labeled as not independently corroborated.
- Next-week availability using the signed-in user's primary calendar; concrete meeting analysis; matching existing meetings using both interval and attendee evidence; alternate-time reply drafts for conflicts; availability rechecks before approved replies are sent.
- Exact company knowledge lookup, dated source reuse, missing/stale-field refresh, explicit user corrections, optimistic version checks, revision history, and immutable report snapshots. Qdrant is not required for this company knowledge path.
- Exact-action previews, approve/reject/edit, payload version/hash checks, owner checks, expiry, atomic execution claims, duplicate-submission handling, and durable unknown outcomes after uncertain sends or crashes. Trash is implemented; permanent deletion is not.
- Dark responsive company/person report layouts based on the supplied examples, citations beside findings, source excerpts, measured coverage/source counts/latency, a Markdown download, and browser print/save-to-PDF.
- Isolated demo login with two datasets: six user-supplied sample inputs by default, and the original four fictitious messages across three fictitious companies for synthetic scenarios. Neither dataset has Google credentials or calls live/legacy tools, paid models, or search providers.

## User-supplied sample emails

The user's screenshot `Screenshot 2026-10-25 at 8.42.31 PM.png` adds six authoritative sample inputs; see [sample provenance](sample-emails.md) and `src/services/research/fixtures/customer_emails.json`. These are example messages about real company names, not verified company research. The default demo preserves those inputs with explicit placeholders for unsupplied headers, no invented meeting interval, and no invented attachment.

Input-only reports cite the supplied email text, label the missing external research, exclude user-provided facts from researched-field completeness, and do not generate fake adverse news, pricing, people, or scale. The synthetic dataset remains available separately to exercise rich fixture reports and meeting/action cases without assigning invented facts to real companies. Six additional regression cases cover input fidelity, six isolated reports, Samsung's ambiguity, Viettel's absent attachment, dataset isolation, and invalid dataset selection.

## Main modules

| Module | Responsibility |
|---|---|
| `src/schemas/research.py`, `src/schemas/errors.py` | Validated inputs, meeting intervals, email addresses, safe errors. |
| `src/schemas/dossier.py`, `src/schemas/extraction.py` | Cited dossier, specialist section outputs, model extraction contracts. |
| `src/configs/research.py`, `src/configs/research_prompts.yaml` (+ `research_prompts.py` loader) | Environment settings and the research agents' prompts (edited in YAML). |
| `src/services/research/store.py` | Owner-scoped SQLite records, company revisions, idempotency, transactional action claims, restart recovery. |
| `src/services/research/backends/` | Explicit live Google/Tavily clients (`live.py`) and in-process demo providers (`demo.py`). |
| `src/services/research/web_fetcher.py` | Public-only URL validation, pinned DNS connections with TLS hostname validation, bounded same-host crawling and source extraction. |
| `src/services/research/reasoner.py` | Tool-free Anthropic extraction with schema validation, deadline, and model-call budget. |
| `src/services/research/dossier_service.py` | Dossier citation validation, fact derivation, scoring. |
| `src/agents/*_agent.py` | The seven role implementations and evidence/calendar/approval policies, plus `fast_research_agent.py` and `search_agent.py`. |
| `src/workflows/research_workflow.py` | Background multi-company execution, calendar only on meeting requests, bounded concurrency, independent outcomes, telemetry. |
| `src/api/research_routes.py`, `src/tools/research.py` | Research/report/knowledge/action routes with owner/CSRF checks; the conversational `research_emails` tool. |
| `frontend/research.html`, `frontend/assets/research.css`, `frontend/assets/research.js` | Inbox selection, company dashboard, person sections, sources, knowledge editing, and action queue. |

Integration changes preserve the existing chat routes and read-only CLI flow. Web OAuth optionally requests Gmail modify and Calendar read-only for the new capabilities. A modify token also satisfies the original Gmail read tools. Browser Calendar calls cannot fall back to another account's local token. The original chat remains separate from the new role coordinator; its model is not given the new mail write methods.

## Implementation decisions

SQLite is the durable store for a single server process. Research uses bounded in-process background work rather than a distributed task system. Company workers are capped at three per batch; website and external research may run concurrently; the calendar read begins alongside email analysis. The runtime store is initialized/recovered once even when the first requests arrive concurrently.

Live search defaults to disabled until an eligible Google account/key/engine is configured. Direct company website retrieval still works. Demo is selected by an isolated demo session, never as an invisible fallback in a live report. Social sources are conditional on actual public text access; login walls or unsupported pages remain explicit limitations.

Defaults and setup are documented in the English README and `.env.example`. No live Google or paid model operation was executed to verify this implementation.

## Verification evidence

- Full offline suite: **166 tests passed**, comprising 151 previous tests and 15 broad-research regressions in `tests/test_research_discovery.py`.
- Ruff: passed for the research modules and `tests/test_research*.py` (layout refactored from `src/research` into `agents/`, `schemas/`, `services/research/`, `workflows/`).
- Python compilation: passed for new modules and modified server/auth/mail/calendar modules.
- JavaScript syntax: `node --check frontend/assets/research.js` passed.
- Git whitespace validation: `git diff --check` passed.
- Browser verification: isolated Chrome profile against the local demo preview; desktop 1440×1000 and mobile 390×844. Verified selecting four emails, producing three company reports, viewing company/key-person sections, following an internal citation, saving a knowledge revision and reading its history, reviewing an alternate-time draft, and approving one simulated send. No JavaScript exceptions or horizontal overflow were observed.

The in-app Browser tool could not start because its sandbox rejected a workspace path containing brackets. Browser verification therefore used a separate local Chrome headless profile, not the user's browsing profile or signed-in accounts.

Additional sample-dataset UI verification covers six company cards, Samsung clarification, the Viettel missing-file notice, mobile overflow, and switching to the separate synthetic dataset. Its local evidence is `/tmp/assignment-customer-samples-ui-qa.json`.

Temporary local verification artifacts:

- `/tmp/assignment-research-final-tests.log`
- `/tmp/assignment-research-ui-qa.json`
- `/tmp/assignment-research-home.png`
- `/tmp/assignment-research-report.png`
- `/tmp/assignment-research-approval.png`
- `/tmp/assignment-research-mobile.png`

These files are local QA artifacts, not committed report data or deployed assets. Screenshot figures measure a simulated run, not real Internet research performance.

### Acceptance coverage

| Criteria | Evidence |
|---|---|
| AC-01, AC-02, AC-14, AC-20 | Offline batch/API tests produce three isolated company reports from four messages; existing MIME/cache tests remain passing; ambiguous identity requires explicit selection; partial company failures preserve successful reports. |
| AC-03, AC-04, AC-11 | Approval concurrency, rejection, cross-owner/forged/expired approval, exact edited payload, duplicate proposal, unknown send, and restart tests; simulated Trash state; Gmail MIME/transport tests confirm no write retries. |
| AC-05, AC-15, AC-22 | Conflict/already-scheduled/available cases, Sunday/year boundaries, missing timezone, all-day/overlapping intervals, calendar failure and changed availability, attendee identity, and separate company action references. |
| AC-06, AC-07, AC-10, AC-16 | URL/private DNS/redirect/size tests; TLS public-IP pinning; deadline and streamed search limits; demo cannot instantiate live clients; no silent fallback; source/provider limitations persist in reports. |
| AC-08, AC-18, AC-19 | Literal evidence/source validation, rejection of wrong-entity and invented quotes, no uncited facts, explicit adverse status and single-source caveat, actual checklist denominator/source counts/date metadata. Browser opens citations and renders the report. |
| AC-09, AC-17 | Owner-separated company records, fresh/missing/stale checks, user provenance, optimistic edit conflicts, revision history, cache reuse, immutable old reports. |
| AC-12 | Auth/consent and browser-account regression suite, demo/live tool separation, owner-scoped report/history/action endpoints, cross-origin/header checks, revoked-login recovery. |
| AC-13, AC-21 | Desktop/mobile browser checks and the company/person fixture report; sourced report layout, missing-data states, visible demo/user labels, no screenshot values injected as measured defaults. |

This is functional/regression evidence, not independent validation of business facts, an accuracy benchmark, a production load benchmark, or a guarantee that inaccessible sources can be retrieved. “No detected contradiction” is never presented as a measured accuracy percentage.

## Operational limits and remaining extensions

Use one server process. Login sessions/credentials are in memory and require re-login after restart; real-account records remain in SQLite. Interrupted research is marked failed, not silently resumed. An uncertain action is not replayed and needs provider-side reconciliation. Records are locally retained until explicitly removed; automatic retention/deletion is not implemented.

The confirmed initial workflow is user-triggered. Inbox polling/provider notifications, timed pre-meeting triggers, outgoing attachments, dedicated Word/PDF generation, official-release queues, external notifications, multi-instance workers, and durable login sessions remain separate extensions. Browser print-to-PDF is available; it is not a server PDF renderer. No private contract store was provided or connected.

No deployment, Git commit/push, live email send, or live Trash operation was performed by this implementation task.

### Main application integration

The server now runs on the existing `http://localhost:9004` origin. After authentication, `/` routes to the research workspace; the original conversational assistant is available at `/chat` and linked from the workspace. Demo sessions remain isolated from live chat tools. The Google OAuth callback still uses `/` and consumes its authorization parameters before routing. Offline regression checks remain **133 passing tests**. Browser verification against port 9004 confirmed six company reports, Samsung clarification, the Viettel missing-file notice, dataset switching, no mobile overflow, and no console errors; local evidence is `/tmp/assignment-main-9004-samples-ui-qa.json`. No live Google writes or paid model calls were made during verification.

### Conversational workflow and Tavily

Chat now registers `research_emails` through the normal YAML tool registry. A request-scoped context binds it to the browser's signed-in owner/backend and the same SQLite/workflow used by `/research`; it never falls back to CLI credentials. The latest-message count is bounded to 1–20. Repeated tool calls reuse one batch per turn. Its result contains bounded per-category evidence/citations, calendar availability/limitations, unresolved identities, role outcomes and private report URLs. The Chat API separately returns report cards so the user can open detailed reports even if model prose omits a link. DOM text and validated relative report URLs are used rather than injecting model HTML.

The user selected Tavily and added its key to `.env`. The adapter POSTs only company name/topic to the fixed HTTPS Search endpoint, uses basic depth/five results, disables redirects and proxy inheritance, caps response bytes/deadlines, sanitizes provider errors and shares a three-search budget per company. Secrets are absent from tool results/config responses and the Tavily settings field is excluded from repr. No Anthropic server-side Web Search adapter was enabled.

For missing company URLs, discovery reads candidate pages, excludes third-party domains without company-brand correspondence, and requires a model's official-page decision plus literal company-name evidence. Multiple verified domains remain unresolved. Discovery reads consume the same page budget. User overrides in Chat must appear literally in the current message. This identifies the company to research; it does not prove that a personal-address sender represents it.

Verification: **151 offline tests passed** (133 previous + 18 new Chat/Tavily/discovery tests). They cover the exact Chat request through the real tool registry and workflow with fake providers, report ownership, missing calendar permissions, one-batch scope, guessed-domain rejection, authentication/no-mail errors, Tavily response/auth/quota/redirect/size/deadline handling and three-call budgets. A real model call on the user's exact request selected `research_emails` with `latest_count=2`; no Gmail handler was executed in that check. Live Tavily public FPT Software queries succeeded. Browser QA used an isolated temporary fake-provider server and confirmed two clickable report cards, correct tool badge, report opening, no mobile overflow and no console errors. Evidence: `/tmp/assignment-chat-workflow-tests.log`, `/tmp/assignment-chat-workflow-ui-qa.json`, `/tmp/assignment-chat-workflow-desktop.png`, `/tmp/assignment-chat-workflow-mobile.png`. No real email send/Trash or calendar mutation was performed.

### Broad multi-source research (latest)

The user's request to research company information beyond one website is implemented in `src/services/research/discovery.py` and integrated into the same `/research` and Chat workflow. Eight fixed public topic queries cover profile, products/pricing, founders/leaders, scale/financials, customers/partners/projects, public professional/social profiles, recent news and adverse information. All queries are planned before page reads; page selection proceeds in rounds with a maximum of two search-result pages per topic/hostname. Actual fetch concurrency is three per company. The official-site crawler prioritizes different relevant section groups. Failed reads consume the page budget, and blocked/private/redirected/unreadable pages do not become evidence.

Default settings now allow 24 page attempts per company (including identity reads), ten search calls, up to eight official-site pages, and a 240-second batch deadline. Source extraction processes every collected page in batches of up to six while retaining the existing twelve-model-call cap, literal quote checks and entity matching. New `financials` and `partners` categories are available in stored/user-edited facts, report cards and Chat synthesis. No private email excerpt was added to search queries or the conversational bridge.

Company knowledge stores a versioned `research_scope`: per-topic query outcomes, source IDs read, actual source metadata/domain diversity and configured bounds. Reports and Markdown exports show this metadata independently of the unchanged six-field evidence checklist. Live cache records from the earlier narrow plan require refresh; existing report snapshots keep their original content. Broad-plan cache reuse is still subject to the existing field/evidence/freshness checks.

Verification: **166 offline tests passed** (151 prior + 15 new). New tests cover eight-topic fairness, multiple domains, canonical URL de-duplication, a two-page hostname cap, zero/deadline/failed-read budgets, preserving other topics after provider failure, source classification, bounded configuration, evidence beyond the tenth page, partial model-batch failure, section-balanced crawling, persisted scope, old-cache refresh and valid-cache reuse. A real public FPT Software check completed eight Tavily queries and read five pages from five domains; two blocked redirects and one fetch failure remained explicit errors. No Gmail access or mailbox mutation occurred in this check. Local evidence: `/tmp/assignment-broad-research-tests.log`, `/tmp/assignment-broad-research-live.json`, `/tmp/assignment-broad-research-ui-qa.json` and report-scope screenshots. The main app on port 9004 remains stopped as the user requested; UI QA uses a disposable fixture server.
