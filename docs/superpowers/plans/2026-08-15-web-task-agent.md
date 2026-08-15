# Web Task Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing fixed Web workflow into a bounded, tool-using Web task Agent backed by OpenCode Go.

**Architecture:** Keep the existing FastAPI/Celery/AgentRunner architecture. Add configuration and worker wiring for OpenCode Go, add an approved HTTP request tool, and make the planner/critic cycle replan from persisted observations without introducing a second runtime.

**Tech Stack:** Python 3.12, FastAPI, Celery/Redis, httpx, Pydantic, pytest.

## Global Constraints

- Preserve exact-host URL allowlisting and public-address SSRF checks.
- Never persist API keys, cookies, authorization headers, or raw sensitive request/response bodies.
- Keep model calls, steps, replans, output tokens, and task timeout bounded by existing settings.
- Use the existing `deepseek` route name; OpenCode Go is a backend configuration, not a new UI provider.

### Task 1: Runtime wiring and provider mode

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `backend/secagent/config.py`
- Modify: `backend/secagent/providers/deepseek.py`
- Modify: `backend/secagent/providers/__init__.py`
- Test: `backend/tests/unit/test_job_queue.py`
- Test: `backend/tests/unit/test_config_secrets.py`
- Test: `backend/tests/unit/test_deepseek_provider.py`

- [ ] Add failing assertions for `secagent.worker`, configurable `MODEL_MODE`, endpoint/model propagation, and OpenCode Go's `reasoning_effort` payload.
- [ ] Implement configuration and Compose wiring.
- [ ] Run the focused unit tests and confirm they pass.

### Task 2: Approved HTTP request tool

**Files:**
- Create: `backend/secagent/tools/http_request.py`
- Modify: `backend/secagent/tools/web_tools.py`
- Modify: `backend/secagent/agents/scenes.py`
- Modify: `backend/secagent/main.py`
- Modify: `backend/secagent/worker.py`
- Test: `backend/tests/unit/test_http_request_tool.py`

- [ ] Add tests for GET, approved non-GET method, redirect revalidation, body limits, and redacted persistence-shaped output.
- [ ] Implement bounded HTTP requests with exact URL checks and safe observations.
- [ ] Register the tool in API and Worker registries and expose it to the Web scene.

### Task 3: Bounded replanning loop

**Files:**
- Modify: `backend/secagent/agents/planner.py`
- Modify: `backend/secagent/agents/critic.py`
- Modify: `backend/secagent/agents/runner.py`
- Modify: `backend/secagent/services/task_service.py`
- Test: `backend/tests/integration/test_web_agent_replanning.py`

- [ ] Add a failing integration test where the first critic response is incomplete and a second plan completes the task.
- [ ] Pass a bounded, redacted observation summary into subsequent planner calls.
- [ ] Persist replan rounds in the existing orchestration checkpoint and stop at `max_replans`.
- [ ] Verify approvals, idempotency, leases, and report evidence remain intact.

### Task 4: Docs and end-to-end verification

**Files:**
- Modify: `docs/deployment.md`
- Modify: `README.md`
- Test: existing backend unit/integration suites and the Web E2E flow.

- [ ] Document OpenCode Go endpoint/model settings and exact authorized-host configuration.
- [ ] Run focused tests, the full backend suite, frontend build/tests, Compose health checks, and one end-to-end task.
- [ ] Review the diff and commit the completed feature.
