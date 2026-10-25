# Seven-Agent Research Workflow State

- Updated: 2026-10-25.
- Spec: `spec.md`, version 0.7; core implementation authorized after 0.4, followed by the user-supplied six-email dataset update.
- Repository baseline: `3af1e74e1bc2c0e7a6de3505fd2a3fcd1ee913f4`.
- Phase: confirmed core implemented locally and verified; proposed reference-table extensions remain separate.
- Latest sample update: six customer emails from the user screenshot are the default input-only demo; the original synthetic scenario dataset is separate. No real-company research, appointment interval, attachment, or sender address is invented. See `sample-emails.md`.
- Chat integration: `/chat` invokes the same workflow via `research_emails`, returns sourced results and private report links; Tavily is the default live search provider.
- Implemented roles: Email, Web Research, Company Info, Calendar, Report Generation, Memory, Human Approval.
- Confirmed boundaries retained: Company Info reads websites; Memory retrieves/updates owned stored knowledge; multiple companies produce separate reports; email sends and Trash require exact human approval; calendar conflicts produce alternate-time drafts; reports include citations and supported adverse information.
- Source scope: public company sites, accessible social/news pages, and Tavily search (user-selected) or optional eligible Google search; no private contract repository assumed.
- Report references: supplied scorecard/company/person screenshots guide presentation, not factual content or benchmark results.
- Technical choices: SQLite, a single process with bounded background work, isolated demo providers, configurable live search, company/person report pages, Markdown download, and browser print-to-PDF.
- Verification: 166 offline tests passed (151 previous + 15 broad-research regressions); new-code Ruff, Python compilation, JavaScript syntax, whitespace checks, and desktop/mobile browser flow passed. See `implementation.md`.
- Live verification: no live mailbox mutation was executed. Tavily was verified with public FPT Software searches; the existing model was checked for correct tool selection using the exact user request, and public website identity checks were exercised. Full Gmail-to-research behavior was tested with fake providers.
- Application changes: `src/agents`, `src/schemas`, `src/services/research`, `src/workflows` (formerly `src/research`), research UI (`frontend/`), server/login integration, optional OAuth scopes, browser-calendar identity, README/config/dependency updates, and acceptance tests.
- Remaining extensions: unattended inbox/meeting triggers, attachments, dedicated Word/PDF renderers, official-release/notification workflows, durable login sessions, automatic retention, and multi-instance deployment.
- Git delivery: local working-tree changes only; no commit/push/deployment in this implementation task.

## Revision History

- 0.1: seven roles, approved Trash/send behavior, cited report page, source-grounded implementation gaps.
- 0.2: manager scenario, next-week availability, public research, user knowledge revisions, adverse findings, scorecard interpretation.
- 0.3: explicit multi-company isolation and adaptable company/person presentation references.
- 0.4: explicit Company Info/Memory boundary retained; agent-role screenshot reconciled and extensions distinguished from confirmed core.
- 0.5: six user-supplied email inputs, input-only report provenance, and separate customer/synthetic demo identities.
- Implementation: the user requested implementation after version 0.4. Core behavior implemented and verified; evidence and operational limits recorded in `implementation.md`.

## Authorization Record

2026-10-25: the user wrote “rồi hãy implement giúp tôi” following specification 0.4. This authorizes the core implementation described above; it is not approval of any concrete live email action or deployment. Such email actions still require the application's own exact-action approval.

- 0.6: user-authorized Chat-to-workflow orchestration and Tavily search; public official-page verification; private report links in Chat.

2026-10-25: the user requested combined-agent research from Chat, explicitly chose Tavily, and confirmed adding its API key locally. This authorizes the Chat bridge and Tavily public company-name/topic queries. No live email action is approved by this change.

- 0.7: the user requested broad company research across multiple sources. Added eight public query groups, fair page/domain allocation, diverse official-site crawling, all-page extraction, financial/partnership fields, visible/exportable coverage and narrow-cache refresh. Defaults: 24 page attempts, ten searches and a 240-second batch deadline. The main server remains stopped following the user's explicit request to stop port 9004; verification uses temporary isolated services.
