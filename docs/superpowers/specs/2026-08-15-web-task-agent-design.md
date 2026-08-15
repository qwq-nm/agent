# Web Task Agent Design

**Goal:** Let an operator submit an authorized Web security task in the SecAgent-X console and have the system plan, execute bounded HTTP tools, replan from observations, and produce an evidence-backed report.

## Architecture

OpenCode Go is used as an OpenAI-compatible model backend, not as a remote execution runtime. SecAgent-X keeps ownership of the AgentRunner, task lifecycle, risk approvals, tool allowlists, evidence ledger, and report generation. The first useful slice is Web analysis: guarded HTTP requests plus response inspection, with bounded replanning.

The execution flow is:

1. Parse the task and select the Web scene.
2. Ask the model for a minimal plan over the scene's allowed tools.
3. Execute each step through the risk gate and persist its result.
4. Ask the critic whether the evidence is sufficient.
5. If evidence is missing, ask the planner for another bounded plan using the observed results, up to `MAX_REPLANS`.
6. Generate a report containing only persisted evidence.

## Safety Boundaries

- Every URL and redirect is checked by `UrlGuard`.
- Host access is an exact allowlist for explicitly authorized private/demo hosts; public hosts still pass global-address checks.
- Non-GET HTTP requests are medium risk and require the existing approval flow.
- Request headers, cookies, bodies, and response data are redacted before ledger persistence.
- Step, model-call, token, and timeout budgets remain enforced.

## OpenCode Go Compatibility

The DeepSeek provider keeps its internal name for stage routing, but accepts the OpenCode Go endpoint and model through configuration. Official DeepSeek requests retain the `thinking` option; OpenCode Go requests use `reasoning_effort`.

## Verification

Unit tests cover Compose wiring, provider payload selection, URL/redirect enforcement, request redaction, and replan limits. Integration tests cover a Web task that performs an approved request, replans after an incomplete critic result, and persists the final report.
