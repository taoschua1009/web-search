# Security of the AI agent

Reviewed against two OWASP cheat sheets:
[AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html) and
[LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).
Principle: **the model is never the security boundary.** Text screening is a signal; permissions,
limits and approvals are enforced in code outside the model.

## Findings fixed in this review

| Finding (before) | Risk | Control now | Where |
|---|---|---|---|
| `read_file` read **any path** the model named (`.env`, OAuth secrets, session key) | Data exfiltration after an injected instruction | Folder allowlist (`READ_FILE_ALLOWED_DIRS`, default Documents/Downloads/Desktop), symlink/`..` resolved first, credential files always refused, 25 MB cap | `services/file_reader.py` |
| Agent loop was `while True` | Denial of wallet, recursive tool abuse | 15 LLM turns, 30 tool calls, 300k tokens per request; history bounded to 60 messages | `agents/agent.py` |
| Long-term memory shared by **all users**, no limits | Memory poisoning, cross-user leak | Entries owned by the signed-in person, 90-day expiry, 2000-char cap, instruction-like text rejected, secrets redacted, results returned as untrusted data | `tools/memory.py`, `services/vectorstore.py` |
| External content (mail, web, files, memory) fed to the model as plain JSON | Indirect prompt injection | Results of such tools are wrapped in an `untrusted_content` envelope with handling notice and `security_flags`; research evidence goes in `<untrusted_source>` tags the page cannot close | `registry/guardrails.py`, `agents/fast_research_agent.py` |
| Injected content could lead to a state-changing tool call | Goal hijack, unauthorised action | After a flagged tool result, every non-read-only tool is refused for the rest of that request | `agents/agent.py` |
| Schema check: types/enum only; `True` passed as integer; `additionalProperties`, `maxLength`, `maximum` ignored | Tool parameter abuse | Full enforcement of the manifest keywords in the registry | `registry/registry.py` |
| Tools unclassified; unknown risk ran | Excessive agency | `risk: low/medium/high` in every tool YAML; high or unclassified tools are refused (fail closed) | `configs/tools/*.yaml`, `registry/registry.py` |
| One rate-limit bucket for all web users; unbounded chat | Cross-user lockout, cost | Per-person tool limit; 12 chats/min per person; 5 chat sessions per login; message ≤ 8000 chars; session id pattern | `registry/registry.py`, `api/server.py` |
| Audit log printed/stored raw arguments (addresses, bodies, keys); unbounded | PII/secret leakage, memory growth | Arguments masked and truncated; bounded to 2000 entries; risk, principal and flags recorded | `registry/guardrails.py` |
| Replies unchecked | System-prompt leak, secret or beacon in output | Canary + leak patterns replace the reply; secrets stripped; remote markdown images removed | `registry/guardrails.py` |
| Approval decisions not logged | No forensics | Structured, content-free line and counter per decision/refusal | `agents/approval_agent.py` |
| No browser hardening; `/api/audit` could create agents; stack text sent to browser | XSS/clickjacking blast radius, DoS, info leak | CSP, `X-Frame-Options`, `nosniff`, COOP, Permissions-Policy; audit is read-only; errors generic | `api/server.py` |

## Prompt-injection screening (signal, not boundary)

`scan_untrusted` screens a *normalised copy* (NFKC, invisible and Unicode-tag characters removed,
Cyrillic/Greek look-alikes folded), decodes Base64 blobs for inspection, matches phrases rather
than single words (so "how do I delete my account" is not flagged), and fuzzy-matches scrambled
override phrases ("ignroe all prevoius..."). Input is cut at 50,000 characters first.
On the bundled corpus (`tests/test_agent_security.py`): 16/16 attacks flagged, 0/9 benign flagged.
That is a smoke test, not a benchmark.

## Controls that already existed (kept)

Human approval bound to the exact payload hash, version, actor, expiry and an atomic claim;
idempotent writes (event ids derived from content); read-only research agents with schema-validated
output and citation checks; SSRF-safe fetcher; PKCE OAuth, `HttpOnly`/`SameSite` cookies, CSRF
header on writes; encrypted login-session store.

## Residual risks (not fixed)

* Screening is heuristic. Letter-spaced text (`i g n o r e ...`), other languages and novel
  phrasing evade it; the structural controls above are what contain those cases. A model-based
  guardrail (Llama Guard, Prompt Guard, ...) for input, output and proposed actions is the next step.
* Web chat sessions share one service principal (`sk-admin-001`, defined in source for development)
  with all tool scopes. Rate limits and audit are per person, but scopes are not. Move keys to
  environment/secret storage and derive per-session scopes from the Google grants.
* Approval is a single click. Cheat sheet "step-up authentication" for critical actions (e.g. sending
  e-mail to a new external recipient) is not implemented.
* CSP keeps `'unsafe-inline'` for scripts because the chat and login pages use inline scripts;
  moving them to files would allow a strict policy.
* Dependencies are lower-bounded, not pinned, and there is no CI gate or `pip-audit` run.
* Memories saved before owner scoping have no owner and are no longer returned (deliberately).
* `OTEL`/stdout logs still contain tool result previews (masked and truncated) and message text
  in the console transcript; do not ship console output to a shared collector without review.

## Operating notes

* `READ_FILE_ALLOWED_DIRS` (os.pathsep-separated) controls where `read_file` may read.
* `MEMORY_TTL_DAYS` sets memory expiry (default 90).
* Watch the `agent.security.events` counter: `injection_in_tool_result`, `blocked_after_injection`,
  `run_limit_reached`, `output_validated`, `approval_refused`.
* Re-run `tests/test_agent_security.py` after any change to prompts, tools, memory, retrieval,
  policies or the model provider.
