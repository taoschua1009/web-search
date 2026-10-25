# AI Agent — Assignment 1

A local assistant with a seven-role company research workspace, cited reports, company knowledge, calendar availability, and human-approved email actions. The original Drive/Gmail chat and RAG tools remain available.

## Project structure

```text
Assignemnt-1/
├── .env
├── .env.example
├── src/
│   ├── agents/                 # Chat agent + one module per research role
│   │   ├── agent.py            #   original tool-calling chat agent
│   │   ├── email_agent.py, web_research_agent.py, company_info_agent.py,
│   │   │   calendar_agent.py, memory_agent.py, report_agent.py, approval_agent.py
│   │   ├── fast_research_agent.py   # parallel search + specialist extraction (default)
│   │   └── search_agent.py          # tool-loop agent ("deep" mode)
│   ├── api/
│   │   ├── server.py           #   app, auth middleware, chat API
│   │   └── research_routes.py  #   research/report/knowledge/action routes
│   ├── cli/main.py
│   ├── configs/
│   │   ├── config.py, research.py (research settings), prompts.py, tool_loader.py
│   │   ├── prompts.yaml, research_prompts.yaml (+ .py loader): all prompts
│   │   └── tools/<tool_name>.yaml
│   ├── observability/
│   ├── registry/
│   ├── schemas/                # Pydantic contracts: research, dossier, extraction, errors
│   ├── services/
│   │   └── research/           #   fetcher, search client, SQLite store, reasoner,
│   │       ├── backends/       #   dossier validation/scoring, runtime, fixtures,
│   │       └── fixtures/       #   live (Google/Tavily) and demo backends
│   ├── tools/                  # Tool handlers (research.py = research_emails)
│   └── workflows/              # research_workflow.py: multi-company orchestration
├── frontend/                   # All browser code
│   ├── index.html, login.html, research.html
│   └── assets/research.css, research.js
├── secrets/                    # OAuth client + tokens (git-ignored, mode 700)
├── deploy/docker/docker-compose.yml
├── scripts/start.sh
├── scripts/start.ps1
└── tests/
```

Dependencies point one way: `api`/`tools` -> `workflows` -> `agents` -> `services`
-> `schemas`/`configs` (enforced by `tests/test_architecture.py`). The workflow wires
the roles together; the only role-to-role import is the approval role reusing the
calendar role's availability recheck.

The agent, HTTP API, CLI, and telemetry live in separate packages under `src/`.
Application settings are defined in `src/configs/config.py`, and the system prompt
is stored in `src/configs/prompts.yaml`. Each tool has its own YAML file under
`src/configs/tools/` with `name`, `description`, `input_schema`, and `required_scopes`.
Handlers and their bindings remain in `src/tools/`; the YAML files contain no
executable code. Tool definitions are validated at startup, including filename/name
consistency, schemas, and scopes. Restart the server after changing a prompt or
tool YAML file to load the updated definitions.

## Run the server

```bash
uv sync --frozen --no-dev   # exact, tested versions from uv.lock (Python 3.12)
```

Dependencies are declared in `pyproject.toml` and **locked in `uv.lock`**. To add or upgrade one:

```bash
uv add <package>                     # or edit pyproject.toml, then: uv lock
uv lock --upgrade-package <package>  # upgrade a single package
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt   # keep pip users in sync
```

`requirements.txt` / `requirements-dev.txt` are generated, pinned exports for `pip` and the start scripts; do not edit them by hand.

If you do not have an environment file yet:

```bash
cp .env.example .env
```

The research demo requires no API keys, Google account, or Qdrant. Live AI extraction uses the Anthropic key. The original chat/RAG tools use Anthropic/OpenAI keys and Qdrant; company knowledge in the research workspace uses SQLite.
The Compose file provides Qdrant and the observability services:

```bash
docker compose -f deploy/docker/docker-compose.yml up -d
```

The configured default model is **Claude Haiku 5.5**, with **low** effort.
Override these settings in `.env`:

```dotenv
LLM_MODEL=claude-haiku-5-5
LLM_EFFORT=low
```

The client sends the effort setting through `output_config.effort`.

```bash
.venv/bin/python -m src.api.server
```

Open http://localhost:9004. Choose **Thử nghiên cứu với email mẫu** on the login page to try the research workspace, or sign in with Google for live email research. After signing in, the root URL opens the research workspace; **Chat** opens the original assistant at `/chat`.

To use the CLI:

```bash
.venv/bin/python -m src.cli.main
```

To start the application with observability services from the project root:

```bash
bash scripts/start.sh
# Windows PowerShell: ./scripts/start.ps1
```

The application loads `.env` from the `Assignemnt-1/` project root regardless of
the current working directory. Set `AGENT_ENV_FILE` to use another environment
file during deployment; relative paths for this setting are resolved from the
project root. Existing process environment variables take precedence over values
in `.env`.

## Security

The agent is hardened against prompt injection, tool abuse and data exfiltration following the OWASP AI Agent Security and LLM Prompt Injection Prevention cheat sheets: see [docs/security.md](docs/security.md) for the controls, the residual risks and the abuse-case tests (`tests/test_agent_security.py`). Two settings matter for you: `READ_FILE_ALLOWED_DIRS` (where `read_file` may read; credentials are always refused) and `MEMORY_TTL_DAYS`.

## Live agent monitor (`/agents`)

Open **Agents** in the top menu (or `http://localhost:9004/agents`) to watch the agents work in real time as a **tree of what is running right now**: team (a chat turn or a research batch; a research batch started from chat hangs under that chat turn) → company → agent → the tools it is calling at this moment (parallel searches are listed side by side). Every agent line shows its model calls, tokens in/out, estimated cost, average model latency and elapsed time. Agents drop out of the tree when they finish; tick **Hiện cả agent đã xong** to keep them. A research team has memory, evidence-search, six parallel specialist agents (own model), calendar (only when an e-mail asks for a meeting) and report agents per company. The page updates through Server-Sent Events (`/api/activity/stream`); `/api/activity` returns the same data as JSON.

Everything is in memory, bounded (40 teams, 30 minutes after finishing), visible only to the signed-in person, and masked like the audit log. It never changes how agents run. Costs use the price table in `src/observability/pricing.py`.

## Company research workspace

Open `/research` after signing in. The **Company Research** link in the original chat opens this workspace too.

You can also open `/chat` and ask: **“tôi muốn bạn nghiên cứu 2 mail gần nhất giúp tôi và viết bài báo cáo giúp tôi”**. The conversational agent calls `research_emails` directly, selects the two latest matching inbox messages, and invokes the same seven-role workflow. It receives cited company facts, next-week calendar results and limitations, then writes its final report in Chat. Each company also has an **Mở báo cáo** link to its private report page. This request researches companies rather than only summarizing message bodies. A batch runs at most once per Chat turn; email sends, Trash and calendar creation are not executed by this tool.

When an email from a personal address mentions a company without a website, the workflow searches for its official site and verifies its identity against a fetched page. The candidate domain must match a company-name token, the identity excerpt must literally occur on the page, and multiple verified domains remain unresolved. If confirmation fails, Chat asks for the website. A user-supplied override must appear in the current message. Neither an email subject nor a search snippet proves that the sender represents that company.

1. Search/select up to 20 emails. Related messages are grouped by company; different companies get separate reports.
2. Click **Nghiên cứu email đã chọn**. Public website research and next-week availability run alongside each other. Ambiguous company identity asks for a name and website.
3. Open a company dashboard for sourced facts, products/pricing, key people where evidenced, news, adverse findings, and calendar availability. Claims cite literal retrieved excerpts; inaccessible/unsupported data stays visible as a limitation.
4. Update company knowledge or review its version history. User additions remain labeled, and old reports keep their original snapshot.
5. Open an alternate-time reply draft or compose a response. Add it to the approval queue, review recipients/subject/body, and approve or reject. Moving an email to Trash uses the same approval process.

The default demo contains the **six sample emails supplied by the user**: FPT Software, Vinamilk, Samsung Electronics Vietnam, Shopee Vietnam, Viettel Solutions, and Bosch Global Software Technologies Vietnam. Inputs are stored in `src/services/research/fixtures/customer_emails.json`. Company names/websites and message text are copied from the provided example; original email addresses, subjects, dates, exact meeting times, and attachment files were not supplied. Addresses ending in `@example.invalid` and display subjects are explicitly generated placeholders. Samsung's “next week” request stays unresolved until an exact interval is provided; Viettel's mentioned capability profile is marked as not supplied.

This dataset displays **input-only reports**. It does not invent company profiles, founder biographies, prices, news, or negative findings for those real companies, and it does not cite an unread company website as research evidence. The report shows missing research and zero researched-field coverage. Calendar availability is separately labeled as simulated.

Live research uses eight query groups: company profile/industry/contacts; products/services/pricing; founders/leaders; scale/public financials; customers/partners/projects; public professional/social profiles; news from the past 30 days; and adverse information. Queries run before pages are selected, and pages are chosen in rounds so one topic cannot consume the entire read budget. Up to two search-result pages are read per topic and per external hostname, with three concurrent external reads per company. The official-site crawler separately prioritizes different sections, such as products, leadership, investor information, contacts and news, rather than filling every slot with similar product links.

Default live bounds are **24 page attempts, 10 searches, up to 8 official-site pages, and 480 seconds per batch** (classic pipeline); the default fast pipeline instead runs about nine searches and up to eight page reads in parallel, then six specialist models in parallel (about 30 seconds per batch in testing). Set `RESEARCH_AGENT_MODE=deep` for the slower tool-loop agent. Identity-discovery reads consume the same page budget. Configure `RESEARCH_MAX_PAGES`, `RESEARCH_MAX_SEARCH_QUERIES`, `RESEARCH_MAX_WEBSITE_PAGES`, and `RESEARCH_DEADLINE_SECONDS` to change them. These bounds are ceilings, not promised source counts. Reports show the eight topic outcomes, all pages read, domain diversity and remaining gaps, separately from fields with cited evidence. The six-field coverage percentage still measures the disclosed core checklist; it is not a percentage of the Internet covered. Every fetched source is considered in model batches of up to six, rather than silently dropping pages after the tenth. Public financials and partnerships have dedicated report cards. Older live caches from the narrower research plan are refreshed; report snapshots remain unchanged.

This is broad public-source research, not exhaustive access to every company record. Search snippets are not full-page evidence, and inaccessible social pages, unsupported files, unconfirmed identity and missing public data remain explicit limitations. Tavily receives only the company name and fixed public research topic; private email bodies/attachments are not added to its queries.

Use **Bộ email mẫu → Tình huống giả lập (3 công ty)** to try the original four invented emails and the conflict/already-scheduled/available meeting scenarios, with sourced fixture dashboards and simulated write results. Switching datasets opens a separate demo identity; previous demo reports are not shared across the two identities. Demo sessions never call live Google tools, the legacy chat, paid models, or actual company websites.

### Creating and deleting calendar events from chat

Ask the chat assistant, for example, **“tạo lịch demo dự án cuối khóa thứ 2 từ 19:30 đến 21:30”**. The assistant only *proposes* the event (`propose_calendar_event`): it checks the calendar for overlaps and queues the exact event in the approval queue (`/research?view=approvals`, linked under its reply). The event is created in Google Calendar only when you press **Duyệt và thực hiện**; no attendees are invited and no notifications are sent. Approving the same event twice never creates a duplicate.

If the new time overlaps an existing event, the card warns with the overlapping events' names and times, offers up to three free slots of the same length (nearest times that day, then the same time on the next days) as one-click buttons that re-propose the event there, and the approve button becomes **Vẫn tạo (bị trùng lịch)**.

Deleting works the same way: **“xóa lịch demo hôm nay”** makes the assistant find the event (`list_calendar_events`) and propose its deletion (`propose_calendar_event_deletion`). The card in the chat shows the exact event; it is deleted only after you approve, only if it is still the event you saw, and without notifying attendees. Approval cards appear directly under the assistant's reply, and a missing permission shows a **Kết nối quyền Google Calendar** button.

Every sign-in asks Google once for Gmail (modify), Calendar read-only and Calendar events, so Calendar never has to be connected separately. If you decline the Calendar boxes on Google's screen, sign-in still works and the chat shows a **Kết nối quyền Google Calendar** button. Sessions saved before this change only have the old, smaller permission: sign in once more.

Login sessions are kept (encrypted with Fernet, file mode 0600, in the git-ignored `.runtime/`) for `LOGIN_SESSION_HOURS` (default 168 = 7 days), so restarting the server does not sign you out or ask for permissions again. Set `LOGIN_SESSION_KEY` to supply your own key; deleting `.runtime/login_sessions.*` signs everyone out.

### Live research and Google permissions

- Enable **Gmail API** and **Google Calendar API** in the OAuth client's Cloud project.
- Use the existing web OAuth configuration and redirect URI described below.
- On `/research`, click **Kết nối quyền Gmail + Calendar** to grant `gmail.modify` and `calendar.readonly`. The modify grant supports reading, sending, and Trash; permanent deletion is not implemented. Existing read-only sessions must reconnect before writes or calendar checks work. Server approval is required even after Google consent.
- Research uses the signed-in user's primary calendar and credentials, never another user's local calendar token.
- Live reasoning uses `ANTHROPIC_API_KEY`, `LLM_MODEL`, and `LLM_EFFORT`. Without a key, website excerpts are still returned with limited extraction; demo never calls an LLM.
- **Tavily is the default live search provider.** Set `TAVILY_API_KEY` in the project's `.env`; optionally set `RESEARCH_SEARCH_PROVIDER=tavily` explicitly. Only the company name and public research topic go to Tavily, not whole email bodies or attachments. Search uses the fixed [Tavily Search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search), basic depth, five results per query and at most ten searches per company (including official-site discovery). News queries request the past month; dates must still be evidenced by retrieved sources. Snippets are discovery inputs, and supported report claims cite pages actually read. Missing keys, quota/authentication errors and unreadable sources remain explicit limitations. No extra search SDK is required.
- Alternatively set `RESEARCH_SEARCH_PROVIDER=google`, `GOOGLE_SEARCH_API_KEY`, and `GOOGLE_SEARCH_ENGINE_ID` for an existing eligible Google Custom Search account. Set `RESEARCH_SEARCH_PROVIDER=disabled` to disable search while retaining direct company website research. There is no silent switch from failed live search to mock results. Google documents that this API is closed to new customers and existing customers must transition by 2027-01-01: [provider overview](https://developers.google.com/custom-search/v1/overview).

Google writes use the documented [Gmail send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send) and [Trash](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash) APIs. Provider acceptance does not establish delivery/read confirmation. A timeout, uncertain 5xx response, or restart around a send leaves an unknown outcome and is never replayed automatically.

### Storage, limits, and deployment

Run **one server process** for this local deployment. SQLite persists reports, company knowledge/revisions, submissions, approvals, and action outcomes in `.runtime/research.sqlite3` (owner-only file; excluded from Git). Google credentials remain in opaque server-side login sessions. Restarting loses login sessions; signing in again with the same real account restores access to that account's records. Interrupted research is marked failed; uncertain mail attempts remain unknown.

Default bounds: 20 selected emails, three active batches per owner, three company workers per batch, 24 retrieved page attempts and ten search calls per company, a 240-second batch deadline, seven-day company freshness, up to 12 model calls per company, and 30-minute approval expiry. Working hours default to 09:00–18:00 weekdays, using the requested timezone. “Next week” is next Monday through the following Monday, anchored to the request time; a concrete email proposal uses the email's date context. Availability is checked again before sending suggested slots and does not reserve them.

The report offers a Markdown download and browser **In / lưu PDF**. Background inbox monitoring, scheduled pre-meeting triggers, outgoing attachments, dedicated Word/PDF renderers, official-release queues, and external notifications remain the explicitly proposed extensions in the spec; they are not enabled by this release. No private contract repository is assumed. LinkedIn/Facebook are usable only when publicly accessible; blocked pages are reported as limitations.

Persistent data is local. There is no automatic retention/deletion policy in this version; remove the runtime database only when you intend to remove its stored reports/knowledge/action history. Preserve unknown-action records when investigating uncertain sends. Multi-instance workers (login sessions are stored in an encrypted local file, so they are shared only by processes on the same machine) and automatic retention require further deployment work.

### Offline verification

```bash
uv sync --frozen   # includes the dev group (httpx for the test client, ruff)
AGENT_ENV_FILE=/dev/null ANTHROPIC_API_KEY=fixture-key OTEL_EXPORTER_OTLP_ENDPOINT= .venv/bin/python -m unittest discover -s tests -q
node --check frontend/assets/research.js
```

Tests use temporary databases and fake providers. They never send or trash real mail, fetch real company pages, or make paid model/search calls. See `docs/ai-workflows/seven-agent-research/implementation.md` for architecture and verification details.

## Connect your Gmail account

Connecting Gmail in ChatGPT does not grant this local project access to your
mailbox. The project requires its own Google OAuth authorization.

1. Enable the **Gmail API** in the Google Cloud project that owns your OAuth client.
2. Configure Google Auth Platform. If the app is in External / Testing mode, add
   your Gmail account under **Audience → Test users**.
3. Use a **Web application** OAuth client in `secrets/client_secret.json`, or set
   `GOOGLE_WEB_CLIENT_FILE` in `.env` to another OAuth client file. Register the exact
   redirect URI `http://localhost:9004`, without a trailing slash.
4. Open http://localhost:9004 and choose **Sign in with Google**.
5. Select your account and grant read-only Gmail access. After authentication, the
   browser returns to the research workspace. Open **Chat** to talk to the original assistant. Your account address and the **Sign out** button
   appear at the top of the page.

Optional settings in `.env`:

```dotenv
GOOGLE_WEB_CLIENT_FILE=client_secret.json
GOOGLE_WEB_REDIRECT_URI=http://localhost:9004
```

Before sign-in, the chat, audit, and account APIs return `401`.
The session cookie contains only a random identifier and uses `HttpOnly` and
`SameSite=Lax`. Google tokens are held in server memory. Sessions expire after
eight hours or when the server restarts. Signing out invalidates the local
session. Chat history, audit records, and Gmail tokens are isolated by signed-in
session. The original Google Drive and RAG tools still use the project's local
configuration; those services do not yet provide multi-user isolation.

## Sign in to Gmail for the CLI (optional)

Web sign-in does not require this command. When using the CLI
(`.venv/bin/python -m src.cli.main`), run:

```bash
.venv/bin/python -m src.services.gmail_service
```

Select the correct Google account and grant read-only Gmail access in your
browser. The login process waits for up to five minutes. The CLI token is stored
separately in `gmail_token.json` with file permissions `600`. It does not replace
the Calendar token and is excluded from Git. Web sign-in does not read or
overwrite the CLI token.

To sign in to Calendar for the CLI:

```bash
.venv/bin/python -m src.services.calendar_service
```

Optional settings in `.env`:

```dotenv
GOOGLE_GMAIL_CLIENT_FILE=credential.json
GOOGLE_GMAIL_TOKEN_FILE=gmail_token.json
GOOGLE_GMAIL_OAUTH_PORT=8000
```

The CLI uses `http://localhost:8000` as the redirect URI for a Web application
client. If you change the port, update the corresponding redirect URI in Google
Cloud.

All relative credential and token paths are resolved from `src/configs/`.
For example, `GOOGLE_WEB_CLIENT_FILE=client_secret.json` points to
`secrets/client_secret.json`. Use an absolute path (or `SECRETS_DIR`) to store files elsewhere.
The environment file and application resources are resolved from the project
location, independently of the current working directory. If a port is already
in use, choose another registered port or stop the service using it.

After signing in, you can ask the agent:

- "Read and summarize the five most recent emails in my inbox."
- "Find unread emails from the last seven days."
- "Find emails from someone@example.com about invoices."

The preferred tool for reading and summarizing email is
`search_and_read_gmail_messages`. The backend searches for messages and retrieves
their content in a batch, returning the message bodies to the LLM in a single
tool result. A typical single-page workflow uses **two LLM calls**: tool selection,
then summarization. Pagination, error recovery, or additional tools may require
more calls.

Use `list_gmail_messages` for a list or preview, and `read_gmail_message` when a
specific message ID is already known. These tools do not send, delete, or mark
emails as read. Content longer than 20,000 characters is truncated and flagged;
attachments are listed by filename only. Email content needed to answer your
request is sent to the configured Anthropic model.

Gmail retrieves content using `format=full` in batches of up to 20 messages per
page, rather than fetching metadata for each message and then downloading its
content again. During one `Agent.run`, batch results are cached temporarily so
`read_gmail_message` can reuse them. The search-only tool still returns headers
and snippets to the model. Temporary data is cleared when the run ends, including
on errors, and is not shared between runs or accounts. Binary attachments are
not downloaded; large text bodies stored separately may require an additional
request when read.

For three ordinary messages, the search-then-read flow uses two Gmail HTTP
requests instead of seven: one search request and one batch request. Gmail still
charges quota for each API call inside the batch. The combined tool reuses this
batch implementation without issuing another HTTP request for each message.
In Tempo, inspect the `gmail.batch_get` span with `gmail.batch_size` and the
`gmail.read_message` span with `gmail.cache_hit` to check actual behavior.

For missing permissions or expired/revoked web tokens, sign out and sign in
again. For the CLI, rerun `.venv/bin/python -m src.services.gmail_service`.
If the API rejects a request, verify that the Gmail API is enabled and that your
account is included in Test users in the OAuth client's Google Cloud project.

The web sign-in page distinguishes client configuration errors, expired login
codes, network errors, missing Gmail permissions, and a disabled Gmail API.
When the API is disabled, the page links to the OAuth client's Google Cloud
project so you can enable it. Server logs record only the failed step and error
code, without OAuth exception contents or callback URLs.

Reference: [Google Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use mock data and do not read a real mailbox or call a model.
