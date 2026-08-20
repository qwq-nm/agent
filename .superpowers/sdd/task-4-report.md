# Web Task Agent Task 4 Report

## Scope

- Plan: `docs/superpowers/plans/2026-08-15-web-task-agent.md`
- Feature range: `fd0d495..8ca425a`
- Final review-fix range: `e3195f8..8ca425a`
- Runtime: FastAPI, Celery/Redis, PostgreSQL, OpenCode Go DeepSeek route,
  GLM parse/report route, and the existing evidence ledger.

Task 4 documents the OpenCode Go deployment settings and verifies that the
bounded Web Agent works through the real Compose stack. No API key was printed
or written to this report; live credentials remain encrypted in PostgreSQL.

## Delivered behavior

- `deepseek` can use the OpenCode Go Chat Completions-compatible endpoint while
  retaining the existing provider name and PLAN/CRITIC stage routing.
- `http_fetch` performs bounded authorized HTTP requests, revalidates redirects,
  limits request/response bodies, and persists redacted observations.
- Web tasks can replan from persisted, redacted observations up to `max_replans`.
- Model and tool calls are bounded by the task's remaining deadline.
- Only `http_fetch` transport/timeout failures are eligible for controlled
  replanning; other failed tool results use the normal failure path.
- Medium-risk approvals are scoped to the exact task step, so a replan that
  changes parameters requires a new approval.
- Critic output that identifies only downstream report generation as missing is
  normalized complete; factual evidence gaps remain intact.

## RED/GREEN evidence

- Replanned HTTP parameters initially reused an old same-tool approval and ran
  the second URL. After the fix, the task returns to `waiting_human` and only the
  originally approved URL executes.
- A three-second fake tool initially exceeded a one-second task timeout. After
  the fix, execution is cancelled within the task budget and the task records a
  deadline budget failure.
- A non-transport Web tool failure initially entered the critic/replan loop.
  After the fix, it raises its original error without a critic or second plan;
  `http_fetch/http_transport_error` still replans and completes.
- A critic response whose only missing item was `Generate the final report`
  initially exhausted `max_replans=0`. After normalization, the same task enters
  Reporter and completes.

## Independent review fixes

The first review found four issues in the live-loop implementation:

1. Approval checks were scoped only by task and tool name.
2. Tool execution could outlive the task deadline.
3. Every failed Web tool result could trigger replanning.
4. Report-only critic guidance was prompt-only.

Commit `737d48f` addressed the first four findings. Follow-up review then found
mutable step-row approval reuse and contradictory critic completion handling;
commits `db6c846` and `8ca425a` fixed both. The final independent review found
no Critical, Important, or actionable Minor findings.

## Verification

- Focused recovery/Web Agent/critic contract set: 25 passed.
- Full backend: 273 passed, 91.11% coverage (required minimum 85%).
- Frontend: 9 files and 41 tests passed.
- Frontend production build: TypeScript check and Vite build passed.
- Compose: API, PostgreSQL, and Redis healthy; worker ready; frontend and demo
  target running.
- Probes: `/api/health/live` returned 200 and `/api/health/ready` returned 200.
- Live internal task `c4cc7d25-c8b9-4af8-839f-112fcc4c4d52` completed after one
  approval with 4 model calls, 4 tool calls, 6 evidences, and 1 report.
- `git diff --check` passed for the final review fixes.

## External target observation

The supplied external CTF endpoint resolved and accepted TCP connections, but
repeated HTTP attempts returned an empty response/transport error. The live task
performed bounded attempts and replans, then ended `failed_retryable` at
`MAX_REPLANS`. This validates failure bounds but cannot establish the remote
challenge's expected answer while the endpoint returns no HTTP response.

## Known warnings

- Full backend tests emit the existing Starlette TestClient/httpx deprecation
  warning.
- Test-only SQLite engines emit connection `ResourceWarning` messages during
  garbage collection. They do not fail assertions or affect the PostgreSQL
  Compose run.
