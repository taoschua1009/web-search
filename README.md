# AI Agent — Assignment 1

A local agent with Google Drive, Google Calendar, read-only Gmail access, and RAG memory.

## Project structure

```text
Assignemnt-1/
├── .env
├── .env.example
├── src/
│   ├── agents/agent.py
│   ├── api/server.py
│   ├── cli/main.py
│   ├── configs/
│   │   ├── config.py
│   │   ├── prompts.py
│   │   ├── prompts.yaml
│   │   ├── tool_loader.py
│   │   └── tools/<tool_name>.yaml
│   ├── observability/
│   │   ├── telemetry.py
│   │   └── grafana/, otel-collector.yaml, prometheus.yml, tempo.yaml
│   ├── registry/
│   ├── services/
│   ├── static/
│   └── tools/
├── deploy/docker/docker-compose.yml
├── scripts/start.sh
├── scripts/start.ps1
└── tests/
```

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
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

If you do not have an environment file yet:

```bash
cp .env.example .env
```

Set your Anthropic and OpenAI API keys in `.env`. Qdrant is required for memory.
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

Open http://localhost:9004. The page requires Google sign-in before opening the chat.

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

## Connect your Gmail account

Connecting Gmail in ChatGPT does not grant this local project access to your
mailbox. The project requires its own Google OAuth authorization.

1. Enable the **Gmail API** in the Google Cloud project that owns your OAuth client.
2. Configure Google Auth Platform. If the app is in External / Testing mode, add
   your Gmail account under **Audience → Test users**.
3. Use a **Web application** OAuth client in `src/configs/client_secret.json`, or set
   `GOOGLE_WEB_CLIENT_FILE` in `.env` to another OAuth client file. Register the exact
   redirect URI `http://localhost:9004`, without a trailing slash.
4. Open http://localhost:9004 and choose **Sign in with Google**.
5. Select your account and grant read-only Gmail access. After authentication, the
   browser returns to the chat. Your account address and the **Sign out** button
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
session. Google Drive, Calendar, and RAG memory still use the project's local
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
`src/configs/client_secret.json`. Use an absolute path to store a file elsewhere.
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
