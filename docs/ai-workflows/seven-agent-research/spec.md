# Seven-Agent Email and Company Research Workflow

## Document Control

- Version: 0.7, 2026-10-25; adds conversational workflow orchestration and user-selected Tavily search; retains the six sample emails and Company Info/Memory split.
- Status: confirmed core implemented after the user requested implementation of version 0.4, with subsequent user-requested updates; reference-table extensions remain proposed. See [implementation and verification](implementation.md).
- Repository: `taoschua1009/web-search`, `main`, baseline `3af1e74e1bc2c0e7a6de3505fd2a3fcd1ee913f4`.
- Target checkout: `/Users/vulcan/proton_x/Agentops Harness/Assignemnt-1`.
- This document records the versioned requirements. The delivered core and remaining extensions are identified in `implementation.md`; original proposed defaults are preserved below for traceability.

## Input Provenance and Authority

| Input | Date/version | Authority and interpretation |
|---|---|---|
| User's seven-agent requirements in this conversation | 2026-10-25 | Authoritative functional scope: email, search, company website research, calendar, report page, company memory, human approval. |
| User clarification: move email to Trash with human approval | 2026-10-25 | Settled; permanent deletion is excluded. |
| User clarification: when busy, draft alternate-time email and wait for approval to send | 2026-10-25 | Settled; no automatic send or calendar changes. |
| User clarification: build a report page containing findings and citations | 2026-10-25 | Settled; this does not require streaming each agent's output. |
| User's manager/FPT example: multiple emails, Internet research, next-week availability, knowledge updates, adverse information | 2026-10-25 | Authoritative scenario. FPT is an example entity, not an actual researched subject or a finding. |
| User clarification of research sources: LinkedIn, Facebook, business websites, news articles | 2026-10-25 | Authoritative source scope. Interpret the earlier reference to internal information/contracts as publicly available information for this version; no private contract repository was supplied. |
| Screenshot `Screenshot 2026-10-25 at 8.04.32 PM.png` attached in conversation; original Desktop filename contains a narrow nonbreaking space | 2026-10-25 | Reference table of sample evaluation results: 98% completeness, no detected official-source contradictions, 42 seconds, 12 sources, news within 30 days, meeting readiness. These are illustrative, not measured system results or confirmed service guarantees. |
| User clarification: the mailbox contains many companies; FPT was only an example | 2026-10-25 | Authoritative general behavior: discover and process distinct companies without a fixed company list. |
| Two attached screenshots, `Screenshot 2026-10-25 at 8.09.53 PM.png` and `Screenshot 2026-10-25 at 8.10.24 PM.png` | 2026-10-25 | Presentation/content references for company dashboard and founder/key-person profile. Actual Desktop filenames contain a narrow nonbreaking space. Text, figures, identities, prices, accomplishments, and relationships shown in the screenshots are unverified sample content, not research evidence. |
| Agent-role table screenshot, `Screenshot 2026-10-25 at 8.15.31 PM.png` | 2026-10-25 | Additional reference: inbox monitoring, email attachments/send status, pre-meeting triggers, multiple report formats, reusable context, approval queue. Its Company Info = DB row conflicts with the user's explicit choice below and does not override it. Original filename uses a narrow nonbreaking space. |
| User answer: keep Company Info reading websites and Memory retrieving stored data | 2026-10-25 | Authoritative resolution of the role conflict; retain the earlier boundary. |
| Six sample customer emails in `Screenshot 2026-10-25 at 8.42.31 PM.png` | 2026-10-25 | Authoritative example inputs for FPT Software, Vinamilk, Samsung Electronics Vietnam, Shopee Vietnam, Viettel Solutions, and Bosch Global Software Technologies Vietnam. No original sender addresses, subjects, dates, concrete appointment intervals, or attachment files supplied. Preserve inputs and missing-data states; do not invent real-company research. |
| Source files and tests in this checkout | Baseline above | Evidence of existing behavior, not evidence that proposed functionality is implemented. |
| Google API documentation linked below | Accessed 2026-10-25 | Provider capabilities and constraints; verify again when implementing a live adapter. |

Product decisions above are confirmed. Technical choices explicitly called proposals below remain reviewable. Earlier audits of a different checkout are not specifications for this feature.

## Summary, Goals, and Scope

Turn selected emails into a company research report page with traceable sources, meeting availability, and reviewable email actions. Provide seven distinct agent responsibilities with restricted tools, coordinated through typed workflow state. Seven roles do not require seven LLM calls for every email.

In scope: bounded research across multiple selected customer emails; company identity extraction; Gmail reading, proposed sends, approved sends, approved moves to Trash; simulated or supported Google search; bounded public company research across official websites, news, and accessible LinkedIn/Facebook content; next-week calendar availability and conflict-response drafts; sourced company memory, user knowledge updates, adverse-information reporting; authenticated report pages and human decisions.

Non-goals for this version: permanent email deletion, automatic inbox sweeps, unsolicited sends, calendar event creation/cancellation/rescheduling, public report publishing, arbitrary website crawling, authenticated website scraping, attachments/OCR/audio/video analysis, changing the LLM framework, or migrating to an internal SDK.

## Current Behavior

| Area | Source evidence | Existing capability and gap |
|---|---|---|
| Agent runtime | `src/agents/agent.py:53`, `Agent.run` at line 81 | One synchronous Claude tool loop with a shared tool list; no seven-role coordinator. |
| Gmail | `src/tools/gmail.py:24`; `src/services/gmail_service.py:31`, `read_message`, `search_and_read_messages` | List/read/batch-read, OAuth read-only; no send or Trash handlers. |
| Calendar | `src/tools/google_calendar.py:16`, `:33`; `src/services/calendar_service.py:28`, `find_free_slots` | List events and find free slots using read-only scope; no email-to-meeting workflow. |
| Memory | `src/tools/memory.py:15`, `:40`; `src/services/vectorstore.py:31`, `:46` | Generic embedding save/search with random record IDs; no exact company-domain identity, freshness, or owner filter. |
| API/UI | `src/api/server.py:123`, `:150`, `:191`; `frontend/index.html` | Per-login chat agents and chat UI; no workflow/report/approval resources. |
| Identity and permissions | `src/services/web_auth.py:26`, `:108`; `src/registry/registry.py`, `USER_DB`, `check_scopes`, `audit_log` | Browser Gmail credentials exist; tool access uses static demo identities and audit entries can contain raw arguments. These are insufficient boundaries for new writes. |
| Instructions | `src/configs/prompts.yaml` | Email text is untrusted; saving email content requires explicit user intent. Preserve these boundaries. |

Existing tests in `tests/test_gmail.py`, `test_gmail_batch.py`, `test_web_auth.py`, `test_model_config.py`, and `test_project_layout.py` are regression coverage, not seven-agent acceptance evidence.

## Proposed Behavior and Agent Contracts

Names below describe role contracts; the delivered class/method/API mapping is recorded in `implementation.md`. They are not all exposed as tools in the legacy chat.

| Agent | Tools/capabilities | Output and boundary |
|---|---|---|
| Email Agent | Existing Gmail reads; `propose_send_email`, `propose_trash_email`; guarded `send_email`, `trash_email` execution | Reads selected messages, prepares an exact action preview. Execution requires a server-validated approval for that payload and mailbox. |
| Web Search Agent | `search_web(query, provider)` through a mock or supported Google adapter | Ranked title/URL/snippet results with provider and mock labels. Results are discovery evidence; substantive claims should cite retrieved pages. |
| Company Info Agent | `resolve_company`, `extract_company_urls`, `fetch_public_page`, `research_company_site` | Resolves company names such as FPT from selected emails, confirms the entity/domain, and assembles source-backed company facts and adverse findings from accessible public sources. |
| Calendar Agent | Existing event/free-slot tools; next-week availability; meeting extraction and comparison | `available`, `conflict`, `already_scheduled`, `needs_clarification`, or `unavailable`; conflict produces alternate-time email text for Email Agent. |
| Report Generation Agent | `build_report`, `get_report` | An authenticated report page with sections, claim-level citations, source list, limitations, and proposed email actions. |
| Memory Agent | `lookup_company`, `upsert_company`, `update_company_knowledge`, `get_knowledge_history`; optional existing semantic search | Reuses owned company knowledge, accepts explicit user corrections/additions, preserves provenance/version history, and refreshes stale or missing public facts. |
| Human Approval Agent | `request_approval`, `record_human_decision`, `get_approval` | Presents approve/reject/edit controls. A human supplies the decision; the agent records it and the server enforces it. |

Tool access is scoped by role. The report, website, memory, and search roles cannot directly invoke email writes. A prompt instruction alone is not the enforcement mechanism.

### Reconciliation with the Supplied Agent-Role Table

The user's explicit response resolves the table's Company Info row: Company Info researches websites; Memory retrieves/manages stored company knowledge and reusable context. Web Search (also called Web Research in the screenshot) discovers Internet sources by topic/entity. These are the same seven roles, not an eighth agent.

The screenshot also describes capabilities beyond the initial selected-email and private-report workflow. Capture them as proposed extensions with the following unresolved contracts; do not represent them as already implemented or enabled:

| Reference capability | Reconciliation and implementation decisions still needed |
|---|---|
| Monitor incoming email and analyze new messages | Add an opt-in ingestion trigger design. Choose polling or provider notifications, mailbox filters, deduplication/checkpoint recovery, and per-company run budgets before enabling unattended runs. Retain bounded user-requested processing as the initial flow. |
| Send email with subject, body, attachments, and delivery status | Preserve approved sends. Attachment support needs allowed formats/sizes and an exact reviewed file manifest; editing attachments invalidates approval. Distinguish provider acceptance from confirmed delivery/read status; no delivery guarantee is implied. |
| Detect upcoming meetings and automatically prepare information | A proposed scheduled research trigger can reuse the existing company/report pipeline. Define preparation lead time, company/attendee resolution, event updates/cancellations, duplicate suppression, and user opt-in. It does not authorize event creation or automatic email sends. |
| PDF, Word, Markdown, and email reports | Keep the cited web page as the confirmed primary output. Record these as candidate export/distribution formats, sharing the same sourced report version. Export priority and exact formats remain open; emailing a report requires approval of recipients, body, and attachments. |
| Memory of context, previous research, results, and user information | Preserve owned company memory and user edits. Additional user/context retention needs explicit data categories and retention/access rules; the screenshot does not authorize copying all private email content into persistent memory. |
| Approval queue for sends, official reports, notifications | Preserve human approve/reject/edit controls and exact-action binding. Distinguish a private research draft from an official released artifact; the latter should enter the approval queue if introduced. Define which notifications are covered before adding external notification tools. |

These proposed extensions require their own acceptance criteria when their product contracts are settled. The existing AC-01 through AC-22 continue to describe the confirmed initial workflow. No trigger, export renderer, attachment sender, or notification channel is activated by this specification update.

### User Flow

1. The signed-in manager selects one or more customer emails or explicitly requests a bounded email search and research run. Group messages only after matching their company identity; retain individual message/thread references.
2. Email Agent reads full available content. It extracts company names, candidate company URLs, people, and meeting requests. A company name without a URL can trigger public search for identity resolution; never fabricate a domain.
3. Resolve the company identity. Do not assume the sender's domain, a tracking link, or every URL is the customer's site. Ambiguous company selection asks the user; approved public URLs pass server validation.
4. Memory Agent checks the exact owner/domain record. Reuse fresh cited facts; Company Info Agent retrieves missing/stale public website content. Web Search Agent may find supplemental sources.
5. Calendar Agent checks the requested next-week window and/or compares a concrete proposed interval with the connected user's calendar. Public research and calendar reads can proceed in parallel once their inputs are valid; calendar access does not wait for website research to finish.
6. Report Agent renders the report page even if one research provider fails, labeling partial results and unavailable facts.
7. Any send or Trash request appears as a separate approval card. Rejection executes nothing. Editing creates a new preview/version requiring approval. No automatic Trash action follows research completion.
8. After approval, the action executor rechecks ownership, permissions, payload hash, expiry, and action state. It executes the exact reviewed action and displays the provider-confirmed outcome or an explicit unknown outcome.

### Multiple-Company Mailbox

The mailbox may contain many unrelated companies, with multiple emails per company. FPT and ProtonX are examples only. Company resolution must operate on discovered entities, with no fixed company whitelist or company-specific report content.

Within the user's bounded email selection/search, resolve company associations per message and deduplicate repeated messages/threads. Create a batch with separate company runs, owned company knowledge, reports, source mappings, and action proposals. A forwarded email or a thread mentioning several businesses must preserve each association's evidence and ask for a primary target when unclear. Do not merge everything in one thread into a single company.

Present a company list with resolved name/domain, relevant message count, research state, last update, and report link. Each report belongs to one company. A failed or ambiguous company remains visible and must not block completed reports for other companies. Cross-company comparisons are additional scope, not an implicit merged report.

Calendar checks use the same user's calendar. A bounded next-week availability snapshot may be reused across company analyses for the same interval/timezone, with its checked-at time displayed. It is not a reservation: multiple proposals can compete for a slot, so recheck each approved reply before send. Recipients, source emails, memory, citations, and approvals stay associated with the intended company.

### Manager Scenario and Public Source Scope

For an example manager receiving several messages from FPT: identify the correct legal entity/business unit, retrieve its existing owned knowledge, then research publicly available company background and relevant recent developments while checking next-week availability. Do not conflate a parent company, subsidiary, branch, or unrelated similarly named entity.

Website/company name, industry, products/services, company scale, news, and public business contacts are the coverage categories from the supplied screenshot. Scale claims need a date and source; a company-reported figure remains labeled as such. Contact coverage concerns publicly listed business contacts, not inferred private contact details.

Use official company pages, accessible LinkedIn/Facebook pages/posts, and news articles. LinkedIn/Facebook access is conditional: use supported accessible content or search-result discovery, show when only snippets were available, and do not claim to have read a login-walled or blocked page. No authenticated social scraping or bypass is implied. Social profile identity must be checked before associating it with the company.

For a contract-related email, research publicly announced partnerships, contract awards, customer relationships, or other published context when available. No access to private contract terms or an internal document store is assumed. Private-document retrieval would be a separate future input with explicit access and storage decisions; public information cannot substitute for a missing private agreement.

### Calendar Rules

- A user request for “next week” means the next Monday 00:00 through the following Monday 00:00 in the user's configured timezone, displayed as explicit dates. Anchor this instruction to the request time. Relative wording inside an older email instead uses that message's context. Ask when a requested time window is ambiguous.
- With no concrete meeting interval, show next-week busy/free windows during configured working hours. Do not label the entire week free after finding one slot. Check the entire authorized window and disclose unavailable calendar data. Only request a duration when producing candidate meeting slots that need one.

- Parse date, start/end or duration, and timezone. Preserve the original text alongside normalized timezone-aware timestamps.
- Missing or ambiguous time, timezone, or duration requests clarification; do not guess a meeting interval silently. Relative dates use message date plus explicit timezone context.
- Check overlapping intervals, all-day busy events, recurring instances, and daylight-saving boundaries. Report unavailable calendar access as unknown, never as free.
- A matching existing meeting is `already_scheduled` only with sufficient identity evidence, such as matching event/thread metadata or interval plus participants. Same time alone is not proof.
- If busy, find alternative free intervals of the required duration, then draft a reply naming dates, times, and timezone. The user reviews the recipient, thread, alternatives, and full email before sending.
- Availability can change while approval is pending. Recheck suggested slots before send; changed availability invalidates the proposal and requires a fresh preview. This does not reserve a slot.
- Default proposal: up to three alternatives within seven days, using configured working hours. These numbers are assumptions, not settled product requirements.

### Company Research and Memory

“All information” means accessible, relevant company information within a declared crawl budget. Cover identity, overview, products/services, industries, public contacts/locations, leadership if available, and dated news where found. Do not invent absent facts or claim exhaustive coverage.

Proposed crawl budget: homepage plus relevant same-host pages, at most 10 pages, depth 2, three search queries, and a bounded total response size. Follow validated redirects; any additional host requires explicit scope validation. Honor site access restrictions and report pages that need JavaScript or login as unsupported in the first version.

Normalize domains conservatively. Keep explicit aliases rather than merging unrelated domains or companies with similar names. A company record contains owner, canonical domain, aliases, source-backed facts, retrieval timestamps, freshness deadline, and record version. Proposed freshness is seven days; show reused data's actual age. Exact identity lookup precedes optional semantic search.

Store researched company facts, not the entire email body. Preserve the existing explicit-user-intent requirement for storing private email content. Cached facts keep original citations; reusing memory must not manufacture a new retrieval date.

The user can add, correct, or mark company knowledge outdated through the report/company view. Every change records actor, time, company identity, previous version, changed fact, and optional supporting source. An unsupported user addition is labeled user-provided; it is not promoted to independently verified public evidence. Conflicting public facts and user corrections remain distinguishable rather than being silently overwritten. Use optimistic version checks to prevent lost updates; retained reports refer to the knowledge version they actually used. Deletion/retention follows the selected privacy policy, not an assumption of permanent history.

### Adverse Information and Risk Findings

Research relevant adverse information as part of the same entity's public-source review. Report only supported findings, with the precise entity, event date, publication date, source, status, and available follow-up/correction or company response. Distinguish an allegation, an investigation, a reported dispute, and a concluded finding. Label analyst inference separately. Do not infer that FPT has any adverse event merely because it is the example company.

Where possible, corroborate material adverse claims with a primary record or independent reporting. Repeated syndicated stories and reposts are not independent corroboration. If only a single source or inaccessible snippet exists, state that limitation and avoid a definitive conclusion. Positive and adverse reports use the same evidence standard; finding negative content is not a quota.

If the bounded research finds none, say “No supported adverse information found in the checked sources/time window” and disclose that scope. This is not a certification that the company has no risks. Prioritize news published within the last 30 days for the freshness section while retaining older material events when relevant and clearly dated. A recently retrieved old article is still old news.

### Report Page and Citations

Use the attached examples as two reusable report presentations: company profile/dashboard and founder/key-person profile linked to the relevant company. The latter may be a section of the company page or a linked detail page; separate navigation is a proposed UI choice. See [Report presentation references](report-patterns.md) for visible sections, evidence rules, layout guidance, and missing-data states. These presentations are generated for any resolved company, not just the example company.

Provide one report page per workflow, with company identity, overview, industry, products/services, company scale, contacts/locations, dated news, adverse information and risks, next-week calendar/meeting assessment, remembered or user-updated facts, proposed next actions, sources/limitations, and a transparent quality summary. Hide or mark empty sections as unavailable; do not fill them with guesses.

Each factual claim references one or more source records containing URL or authorized email/event reference, title, retrieval time, source type, and supporting excerpt. Show citations beside claims and a source list linking to the original page. Distinguish official company statements, third-party information, email context, and remembered information. Inferences are labeled and cite their inputs. A snippet-only source is labeled as such.

Mock search results and any fixture-derived claims carry a visible demo label throughout the page. Fixtures must never appear as live findings. Conflicting or stale sources stay visible with timestamps. Report text and citations are escaped/sanitized; external pages do not provide executable HTML or approval controls.

### Quality Summary from the Screenshot

The screenshot is a sample scorecard, not proof of measured performance. Display actual measured values or explicit “not measured/unavailable” for each report. Never populate the page with the screenshot's sample numbers by default.

| Criterion | Screenshot example | Proposed measurement and honest display |
|---|---|---|
| Information completeness | 98% across website, industry, products, scale, news, contacts | Define a versioned required-field checklist before evaluation. Show evidenced fields / applicable required fields, missing fields, and any justified exclusions. A percentage needs a disclosed denominator; filled text alone is not evidence. |
| Accuracy | No detected contradiction with official sources | Show detected conflicts and checking scope. Unknown or unchecked claims do not count as verified. Use a labeled reference set/human review for an accuracy benchmark; absence of detected conflicts is not proof of 100% accuracy. |
| Report generation time | 42 seconds | Measure server wall time from accepted run to report-ready/partial, excluding the human approval wait. Separate warm/cold and mock/live cases; record sample size and latency distribution before adopting a target. |
| Number of sources | 12: website, LinkedIn, press, internal data, etc. | Count unique actually used cited source records after URL/duplicate normalization; display source categories and access limitations. Internal data appears only if actually supplied and authorized, which it has not been in this version. Do not pad citations to reach 12. |
| Data freshness | News within the most recent 30 days | Track article publication/event dates separately from retrieval timestamps and cache age; disclose unknown dates and whether the requested window was covered. |
| Meeting readiness | Enough information to prepare the meeting | Show company/background coverage, next-week availability, unresolved questions, and report limitations. Readiness can be an explicit reviewer assessment; an automatic rule must be documented and must not conceal missing inputs. |

Incremental streaming, downloadable PDF, public links, a complete visual design, and numerical performance targets are not confirmed requirements. The screenshot establishes scorecard content; proposed initial delivery remains a private report page.

## Architecture and Data Flow

`Selected company emails → company identity resolution → owned knowledge lookup → public research and adverse findings + next-week calendar analysis → cited report page and quality summary → optional knowledge correction / human decision → guarded email action`

Use a coordinator with typed intermediate results and explicit workflow states: `running`, `needs_input`, `report_ready`, `partial`, or `failed`. Approval/action state is separate so a report remains readable while a decision is pending.

Proposed Pydantic contracts:

- `ResearchBatch`: ID, owner, bounded email selection/search, discovered company associations and resolution state, child run IDs, per-company outcomes.
- `ResearchRun`: ID, batch ID, owner ID, company ID, source message/thread IDs, resolved company/entity, selected domain, explicit calendar date window/timezone, stage states, provider modes, deadlines, report/knowledge versions, measurement timestamps.
- `KeyPersonProfile`: stable person ID, company affiliation with dates/evidence, public professional role, cited career/achievements/profile links, explicit supported professional relationships, uncertainty and retrieval dates. Name alone is insufficient to merge identities.
- `SourceRecord` / `CitedFact`: stable source IDs, validated URLs/references, excerpts, timestamps, fact text, source references, certainty/limitations.
- `MeetingAssessment`: normalized interval/timezone, status, relevant event references, alternate slots, recheck timestamp.
- `CompanyRecord` / `KnowledgeRevision`: owner/domain/entity key, versioned facts and sources, public versus user-provided provenance, actor/time, previous version, freshness, aliases.
- `RiskFinding`: entity, supported claim, event/publication dates, status, source IDs, corroboration/limitations, response/follow-up if found.
- `QualitySummary`: checklist version, covered/applicable fields, missing inputs, conflict-check scope, actual run duration, unique citation count, news window/date coverage, readiness assessment origin.
- `ActionProposal`: owner/mailbox, type `send_email` or `trash_email`, exact target/payload, version/hash, expiry, workflow/report reference.
- `ApprovalDecision` / `ActionAttempt`: authenticated actor, proposal version/hash, decision time, idempotency key, execution state, provider reference or unknown outcome.

### API, Task, Worker, Provider, Data, and Config Changes

Proposed authenticated routes, additive to existing chat:

| Route | Contract |
|---|---|
| `POST /api/research` | Selected message IDs and optional resolved company selections; returns a batch ID with child company run IDs, including the single-company case. Validate ownership and support a submission idempotency key. |
| `GET /api/research-batches/{batch_id}` | Owned company list, resolution needs, child report links and independent outcomes. |
| `GET /api/research/{run_id}` | Owned workflow status, progress, limitations, report reference. |
| `GET /reports/{report_id}` | Owned rendered report page. |
| `GET /api/reports/{report_id}` | Typed report data and source records. |
| `POST /api/actions/{action_id}/decision` | Approve/reject an exact version/hash; identity comes from authenticated session. Edited actions receive new proposals. |
| `GET /api/actions/{action_id}` | Owned proposal, decision, execution status and safe error. |
| `PATCH /api/companies/{company_id}/knowledge` | Explicit authenticated user edit with expected record version, changed facts, optional citations; validate ownership and record provenance. |
| `GET /api/companies/{company_id}/knowledge/history` | Owned revision history according to retention/access policy. |

Use existing FastAPI and provider clients. Research can run as a bounded background job for a single-instance assignment deployment, with durable state and interrupted-job recovery; no distributed worker platform is implied. Browser polling suffices for initial progress. Existing `/api/chat` responses remain compatible.

Storage proposal: SQLite transactions for owned runs, reports, exact company records, approvals, and action attempts in the single-instance deployment; existing Qdrant remains optional semantic retrieval. Qdrant similarity is not the authority for approval or company identity. A durable store choice must be confirmed before implementation, including multi-instance deployment needs. A stopped worker never automatically replays an uncertain send on restart.

Introduce configurable provider mode, Google credentials only if supported, crawl/search/LLM budgets, HTTP/workflow deadlines, company TTL, calendar timezone/hours/week-window rule, news window, versioned coverage checklist, approval expiry, retention, and state-store path. Keep runtime files and credentials out of Git.

### Vendor Capability and Fallback Policy

| Capability | Proposed provider | Eligibility and fallback |
|---|---|---|
| Language reasoning | Existing configured Anthropic client | Reuse existing framework/config. A provider failure gives a partial/error result, not invented findings. |
| Embeddings | Existing OpenAI embedding service | Optional semantic retrieval; exact company lookup still works when embeddings fail. |
| Web search | Deterministic fixture adapter by default for demo | No Google calls or hidden paid fallback. The report labels simulated results. |
| Live Google search | Explicit supported adapter | Google Custom Search JSON API is closed to new customers; existing customers must transition by 2027-01-01. Do not assume a new account can use it. Select an accessible Google product separately if live search is required. |
| Company, news, and social pages | Bounded HTTP retrieval | Public HTML/text when accessible. Social sources may be blocked, login-walled, or snippet-only; preserve the access status. No automatic browser, login, JavaScript, or paid-provider fallback. |
| Mail and calendar | Gmail / Google Calendar APIs | Account consent required. Missing scopes disable the affected action and preserve readable reports. |

Google source: [Custom Search JSON API overview](https://developers.google.com/custom-search/v1/overview), accessed 2026-10-25. A provider outage never silently changes a live run into a fake result; users may explicitly start a demo run.

### OAuth and SDK Inheritance

The current repository uses public Anthropic/OpenAI/Google clients and Pydantic, with no internal `ai-hub-sdk` dependency. Reuse this stack; no SDK migration is proposed.

Gmail read-only cannot send or move messages to Trash. Use feature-specific consent: read-only for reading; `gmail.send` for sending if Trash is disabled; `gmail.modify` when enabling Trash, which also permits reading/sending. Do not request redundant scopes or full `https://mail.google.com/` access. Google permission and application approval are separate checks. Existing users must grant the newly needed scopes before writes become available.

Authoritative references: [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and [messages.trash](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash), accessed 2026-10-25. Calendar remains read-only and must belong to the signed-in user, rather than silently using a shared local token.

## Input and Output Modalities

| Input/output | Normalization and validation | Proposed limits/retention |
|---|---|---|
| Email text/HTML | Reuse MIME decoding, strip active HTML, preserve headers/message IDs, flag truncated bodies | Bounded user-selected input; retain references instead of copying bodies into company memory. |
| Public website text/HTML | Validate URL/DNS/redirects; extract text and supporting excerpts | Bounded pages, depth, bytes, and time; retain cited excerpts only. |
| Meeting text/event data | Timezone-aware intervals, participants, original proposal text | No silent timezone/duration inference. |
| User knowledge edits | Typed changed fields, expected version, actor from session, optional source; mark unsupported user assertions | Owner-scoped revision history; no silent verified status. |
| Report page | Structured facts, risk findings, quality measurements, and safe citations; escaped rendering | Owner-only access; retention policy must be selected. |
| Image/audio/video/attachments | Unsupported for this version | Report limitation instead of silently processing. |

## Reliability Contract

Proposed defaults: HTTP timeout 10 seconds per attempt; at most two attempts for retryable reads within a 120-second research deadline; at most 12 LLM calls per run with configured token limits; three parallel public fetches per run. All values are proposals for implementation review, not performance guarantees. Approval waits do not consume a running research deadline; proposed approval expiry is 30 minutes. Surface budget exhaustion as a partial report.

Stable errors: `AUTH_REQUIRED`, `SCOPE_REQUIRED`, `COMPANY_AMBIGUOUS`, `MEETING_AMBIGUOUS`, `URL_BLOCKED`, `FETCH_FAILED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_RATE_LIMIT`, `BUDGET_EXCEEDED`, `DEADLINE_EXCEEDED`, `APPROVAL_REQUIRED`, `APPROVAL_EXPIRED`, `ACTION_CHANGED`, `ACTION_ALREADY_PROCESSED`, and `ACTION_OUTCOME_UNKNOWN`. Include safe user guidance, retryability, and affected stage; exclude tokens and raw exception payloads.

Read retries cover transient network errors, rate limits with bounded Retry-After, and eligible 5xx responses. Do not retry invalid input, denied permission, blocked URLs, or unapproved actions. Missing website/search data should not suppress successful calendar analysis or other sourced sections.

Approval states: `pending → approved | rejected | expired | superseded`. Execution states: `not_started → executing → succeeded | failed | outcome_unknown`. Use an atomic durable claim before a provider write. Concurrent approvals and repeated requests must not cause a second send. Approval binds the exact mailbox, recipients, subject/body/thread or Trash message ID, and version/hash. Approvals cannot be transferred to edited content.

Gmail send is not assumed to support exactly-once delivery. After a timeout or crash around a send, mark the outcome unknown and reconcile provider evidence; do not automatically retry. Repeated Trash requests check the message's current labels and may report already in Trash without another mutation. Persist provider-confirmed IDs when available.

## Security and Privacy

- Resolve a real signed-in owner for every run, memory lookup, calendar access, report, approval, and execution. Static demo role keys are not browser write authorization.
- Validate origin/CSRF protection and enforce ownership server-side for every state-changing endpoint. The LLM cannot supply a trusted actor ID, approval flag, or provider credential.
- Untrusted emails, websites, and search results cannot change tools, recipient lists, approval policy, or instructions. Extracting user-requested research data is distinct from obeying instructions embedded in an email.
- Public fetches allow HTTP(S) only and reject credentials in URLs, localhost/private/link-local/reserved targets, disallowed ports, and unsafe redirect/DNS destinations. Apply validation on every connection/redirect and bound response bytes, decompression, and parse work.
- Store only necessary cited facts and approval/action metadata. Do not log credentials, entire email bodies, full action payloads, or calendar descriptions in ordinary logs/traces. Protect necessary persisted previews with owner access and the selected retention policy.
- No public report access or automatic external publication is part of this scope.

## Observability

Extend existing OTel instrumentation with run/stage IDs, role, provider mode, duration, status, source/page counts, cache hits, budget usage, and stable error codes. Track approval decisions, invalid/expired approvals, write attempts, unknown outcomes, provider errors, partial-report rates, actual report latency, citation counts, field coverage, and freshness gaps. Record knowledge revisions in the access-controlled audit trail. Do not use email addresses, domains, message IDs, or report text as metric labels.

A local dashboard should show stage latency, failures, search usage, and pending approvals. Unknown send outcomes must be visible to the user and operators; deployment alert thresholds remain environment-specific. Audit records identify the authenticated actor and reviewed proposal hash without leaking raw content.

## Acceptance Criteria and Test Strategy

All automated cases use fake mail/calendar/HTTP/search/LLM/embedding providers. No live messages are sent or trashed by CI.

| ID | Measurable acceptance and planned verification |
|---|---|
| AC-01 | A selected-email fixture produces a workflow and report with all seven responsibilities observable; role tests ensure unrelated roles cannot invoke email writes. |
| AC-02 | Reading preserves actual available body content and flags truncation. Existing MIME, batch-cache, and per-account isolation tests still pass. |
| AC-03 | No Gmail write occurs without an unexpired approval for the same owner, mailbox, action, and payload version. Test missing/forged/expired/cross-user approval and modified recipients/body/message ID. |
| AC-04 | Approved deletion invokes Trash, never permanent delete. Rejecting performs zero provider mutations; an already-trashed message produces a clear result. |
| AC-05 | Conflicting meeting fixtures generate free alternate slots and a reply draft; sending waits for approval. Test overlap, recurring/all-day events, timezone/DST, missing duration, identical meetings, unavailable calendar, and changed availability before send. |
| AC-06 | A safe company URL leads to bounded same-host research with source-backed facts. Test tracking/unrelated URLs, multiple-company ambiguity, private-address/DNS/redirect blocking, oversized content, timeouts, inaccessible pages, and prompt injection. |
| AC-07 | Mock mode performs zero real search calls and visibly labels all fixture-derived report content. Live provider denial or failure yields an explicit limitation, never an invisible mock fallback. |
| AC-08 | Every factual report claim has a resolving citation, or is explicitly marked unsupported/unknown. UI tests open citations, distinguish source types, show stale/conflicting data, and reject executable source content. |
| AC-09 | A second run for the same owned normalized domain reuses fresh company facts; stale fields refresh. Different owners/domains never share private records; embedding failure does not break exact lookup. |
| AC-10 | A provider failure preserves other successful report sections and returns stable stage errors. Fake-clock tests enforce retry, page/query/LLM budgets and deadlines. |
| AC-11 | Concurrent approval submissions and repeated action requests cause at most one claimed send attempt. Simulate restart before/after provider calls, ambiguous timeouts, failed reconciliation, rejection races, and changed payloads. No uncertain send is replayed automatically. |
| AC-12 | Reports/actions are inaccessible across users. Auth/consent regression tests cover existing read-only login, missing write scope, wrong calendar identity, and CSRF attempts. |
| AC-13 | The report page renders readable sections, source links, action previews, and pending/rejected/succeeded/unknown states on desktop and mobile. Include long company names, Vietnamese text, missing sections, and keyboard-accessible approval controls. |
| AC-14 | Multiple emails can resolve to one correct company without conflating a subsidiary or similarly named entity; name-only input can discover a candidate official domain. Ambiguity requests confirmation. FPT/ProtonX are examples only, and no fixture implies a factual finding about a real company. |
| AC-15 | A next-week request checks the explicit full calendar window independent of a concrete meeting proposal. Fake-clock tests cover a Sunday boundary, month/year transition, request versus old-email relative dates, timezone differences, partial failures, and a partly busy week. |
| AC-16 | Public website, news, LinkedIn, and Facebook fixtures carry source/access types. Blocked pages remain limitations, snippets are labeled, and no private contract claims appear without a supplied source. |
| AC-17 | A user correction records owner, actor, time, prior version, and provenance. Concurrent stale edits return a version conflict; cross-user edits fail. An unreferenced user assertion remains visibly user-provided and does not rewrite an existing report's cited version. |
| AC-18 | Adverse findings identify the exact entity, dates, allegation/finding status, supporting source, and follow-up where available. Tests cover syndicated duplicates, single-source uncertainty, corrected stories, old events resurfacing, entity mismatch, and no findings within a bounded search. No unsupported negative conclusion is generated. |
| AC-19 | Scorecard values derive from actual run/checklist/source metadata. Fake-clock and citation tests verify denominator, duration, deduplication, publication-date freshness, unknown dates, conflict scope, and reviewer versus automatic readiness. Sample 98%, 42 seconds, and 12 sources never appear as measured defaults. |
| AC-20 | A fixture mailbox containing at least three distinct companies produces separate company runs/reports with correct message associations and no hardcoded company content. Test repeated threads, forwarded mail, subsidiaries, multi-company mentions, an ambiguous company, and one failed provider path. Successful companies remain available. |
| AC-21 | Generated company and key-person views follow the supplied content patterns while adapting to available facts and industry. Test missing price/founder/team data, approximate versus exact metrics, price dates/currency, unsupported relationship claims, profile name collisions, and citations on KPI cards. Screenshot sample values are never inserted as findings. |
| AC-22 | The same user's shared calendar snapshot can inform multiple company runs without mixing recipients, meeting context, knowledge, citations, or approvals. Two company proposals for the same slot are not marked reserved, and availability is rechecked before each approved send. |

Unit tests cover normalization, typed schemas, citation validation, calendar comparisons, company keys, and state transitions. Integration/contract tests cover fake provider transports, API ownership, durable-store transactions, and OAuth scope changes. Load tests use concurrent runs/approval callbacks to verify configured limits, event-loop responsiveness, isolation, and single claims. Visual/browser checks verify the actual report and approval page; current chat behavior remains a regression target.

## Rollout, Rollback, and Compatibility

1. Implement typed contracts, role tool restrictions, and fixture providers without live writes.
2. Add owner-scoped durable state, company retrieval, bounded fetches, calendar analysis, and cited report pages. Review the demo report and failure cases.
3. Add approval controls and executor with fake-provider concurrency/crash tests. Remove raw content from affected audit paths before writes are enabled.
4. Enable optional live providers only after credentials/scopes are configured and the user explicitly authorizes a concrete test action. Existing scopes do not auto-upgrade.

Additive database migrations create versioned owned records; do not bulk-import unowned generic memories into private company records. Rebuild optional Qdrant indexes from owned source records if necessary. Back up durable state before migration. A rollback disables new writes/runs while retaining reports and action attempts; never replay pending or uncertain sends. Keep existing chat/read tools working unless an explicit contract change is reviewed.

## Settled Decisions

- Seven roles with the responsibilities described by the user.
- Explicitly confirmed after the role-table screenshot: Company Info reads company websites; Memory retrieves stored data. Web Research and Web Search name the same discovery role.
- Company Info Agent resolves company names and researches public websites, accessible social content, and news; Memory Agent stores/retrieves company knowledge and records explicit user updates.
- The mailbox may contain many companies. Group emails by resolved entity and generate separate company reports; next-week calendar checks run alongside public research.
- The two new screenshots guide company-dashboard and founder/key-person presentations, with adaptable sections and sourced content.
- Contract context in this version uses publicly available Internet sources; no private contract repository is assumed.
- Reports include evidence-backed adverse findings and the screenshot-inspired quality scorecard, with real measurements or explicit unknown values.
- Email deletion means moving the selected message to Trash after human approval.
- A calendar conflict produces an alternate-time reply draft that requires approval before sending.
- Report output is a page of findings with citations, not a requirement to stream role outputs.
- Human Approval records a real person's decision; model-generated agreement is insufficient.

## Proposed Defaults and Open Questions

These do not block documenting the confirmed requirements, but must be settled or explicitly accepted before affected implementation:

- Reference-table extensions: inbox-monitoring mechanism/filters, pre-meeting trigger timing, email attachment limits/status semantics, export priorities, reusable user-context retention, and official-report/notification approval policy.
- Search: mock-first demo versus live Google integration, subject to actual account eligibility.
- Crawl/freshness/time budgets and calendar working hours/timezone; defaults above are engineering proposals.
- Single-instance SQLite versus another durable store if deployment requires multiple instances.
- Company/report/approval retention and deletion policy; no indefinite private-data retention is assumed.
- Report language and final interaction/layout details; the three supplied screenshots guide the scorecard, company dashboard, and founder/key-person presentation. They do not establish factual report content.
- Required-field checklist, completeness denominator, benchmark reference set, and any numerical latency/source-count targets; screenshot results remain illustrative.
- Creating or updating calendar events would be additional scope; this version only checks availability and prepares email replies.

The user authorized core implementation after version 0.4 and subsequently supplied six sample email inputs incorporated in version 0.6. This does not authorize a concrete live email action or turn the proposed extensions into enabled features; see `state.md`.

## Conversational orchestration and Tavily (0.6)

The user requested that Chat research the latest two real emails through the combined agents and produce a final cited report, rather than an email-only summary. They explicitly selected Tavily for web search and confirmed adding its API key locally. `research_emails` is the conversational entry point to the existing workflow, scoped to the signed-in account. It returns sourced company summaries, calendar results/limitations and private report URLs, with clickable report cards in Chat. It does not execute sends, Trash, approvals or calendar creation. A missing URL may be resolved through public search plus a verified official-page identity; ambiguous identities require user confirmation. Only company names and research topics are sent to Tavily. User-provided website overrides must literally occur in the current user message. The input-only sample dataset remains unchanged.

## Broad multi-source company research (0.7)

The user clarified that research must cover company information across the Internet, rather than a single company website. Web Research now plans eight public query groups: profile/industry/contacts, products/pricing, founders/leaders, scale/financials, customers/partners/projects, public social/professional profiles, recent news and adverse information. Topic searches precede round-based page selection, with domain caps and bounded concurrency. Company Info crawls diverse official-site sections and analyzes all fetched pages in small evidence-validated batches. Reports and Markdown exports show per-topic outcomes, all read pages and missing coverage. Financial and partnership findings have dedicated categories/cards. Default bounds are 24 page attempts, ten queries and up to eight official-site reads per company, with a 240-second batch deadline. These are configurable ceilings, not a guarantee of exhaustive coverage. The earlier six-field evidence checklist remains disclosed and separate from topic search coverage. Older live caches without the broader plan's coverage metadata require refresh.
