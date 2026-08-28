### Task 3: Fixed DeepSeek V4 Flash coordinator boundary

**Files:**
- Create: `backend/secagent/agents/coordinator.py`
- Create: `backend/tests/unit/test_coordinator.py`
- Modify: `backend/secagent/domain.py`
- Modify: `backend/secagent/providers/router.py`
- Modify: `backend/secagent/providers/deepseek.py`
- Modify: `backend/secagent/providers/glm.py`
- Modify: `backend/secagent/providers/mock.py`
- Modify: `backend/secagent/config.py`
- Modify: `backend/tests/unit/test_model_router.py`
- Modify: `backend/tests/unit/test_deepseek_provider.py`
- Modify: `backend/tests/unit/test_glm_provider.py`
- Modify: `backend/tests/unit/test_mock_provider.py`
- Modify: `backend/tests/unit/test_config_secrets.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: Task 1 document validation and Task 2 `AssignmentPolicy`.
- Produces strict bounded context DTOs, `CoordinatorResult`, and `CoordinatorAgent.decompose(...)`.
- Extends `ModelStage` with `decompose`, `subtask_execute`, and `synthesize`, while preserving legacy enum values.
- Extends `ModelResponse` with optional `emulated_provider` and `emulated_model` fields for explicit mock evidence.
- Extends `ModelRouter.complete`: fixed new stages ignore caller preference; `subtask_execute` requires logical preferred provider `glm` or `deepseek`; legacy stages preserve their current fixed behavior.

- [ ] **Step 1: Write provider-routing RED tests**

Add tests that require:

```python
assert FIXED_PROVIDER[ModelStage.DECOMPOSE] == "deepseek"
assert FIXED_PROVIDER[ModelStage.SYNTHESIZE] == "deepseek"
assert (await router.complete(ModelStage.SUBTASK_EXECUTE, request, preferred="glm")).provider == "glm"
```

Also require live/auto decomposition to reject a missing DeepSeek without
calling an available GLM or Mock, and reject any DeepSeek adapter whose
`.model` is not exactly `deepseek-v4-flash`; require caller `preferred="glm"`
not to override decompose/synthesize. Require explicit mock mode to use Mock
only. GLM availability is not a prerequisite for the model call itself; it is
evaluated later by AssignmentPolicy for the proposed worker assignments.

- [ ] **Step 2: Extend stages/providers/router minimally and reach GREEN**

Add enum values without renaming legacy stages. DeepSeek allows `PLAN`, `CRITIC`, `DECOMPOSE`, `SUBTASK_EXECUTE`, and `SYNTHESIZE`; GLM allows `TASK_PARSE`, `REPORT`, and `SUBTASK_EXECUTE`. Use max tokens 4,096 for decompose/subtask and 8,192 for synthesize. Add `DEEPSEEK_V4_FLASH_MODEL = "deepseek-v4-flash"`. In non-mock mode, router fixed-stage validation occurs before provider invocation and raises safe `ProviderUnavailable(provider="deepseek", code=INVALID_SCHEMA, retryable=False)` on model mismatch.

- [ ] **Step 3: Write Coordinator context/request RED tests**

Create these exact strict DTOs:

```text
CoordinatorRecentMessage(role: "user" | "assistant", content: 1..16,000)
CoordinatorAttachment(original_name: 1..255, relative_path: safe path | null,
  content_type: 1..255, sha256: lowercase SHA-256, scan_summary: 1..2,000)
CoordinatorCompletedSubtask(key: decomposition key, provider: LogicalProvider,
  summary: 1..4,000, evidence_refs: <=64 unique strings of 1..128)
CoordinatorEvidence(ref: 1..128, summary: 1..4,000, confidence: 0..1)
CoordinatorTool(name: tool-name pattern `^[a-z][a-z0-9_]{0,119}$`,
  risk_level: existing RiskLevel, description: 1..500)
DecompositionContext(current_message: 1..64,000,
  conversation_summary: 0..16,000, recent_messages: <=40,
  attachments: <=20, settings: existing ConversationSettings,
  completed_subtasks: <=64, evidence: <=128,
  unresolved_questions: <=32 unique strings of 1..1,000,
  available_tools: <=128 unique-name CoordinatorTool values,
  budget: existing TurnBudgetSnapshot)
```

All string fields reject whitespace-only content except the explicitly allowed
empty `conversation_summary`. Tests must prove list/string bounds, no
extras/coercion, a context larger than `budget.max_context_tokens * 4` is
rejected before a model call, and credential-like values in the current
message/summary/tool metadata are redacted from `ModelRequest.user`.

The coordinator call shape is:

```python
result = await CoordinatorAgent(router, AssignmentPolicy()).decompose(
    context,
    plan_version=turn.plan_version,
    registered_tools={"source_scanner"},
    authorized_tools={"source_scanner"},
)
```

The coordinator never trusts a caller-supplied provider-availability set.
`ModelRouter.logical_assignment_providers()` returns configured logical
providers in live/auto mode and `{glm, deepseek}` only in explicit mock mode;
the coordinator passes that derived set to `AssignmentPolicy`.

Before serialization, replace `context.available_tools` with the ordered subset
whose names are in both `registered_tools` and `authorized_tools`.
`ModelRequest.user` is canonical compact JSON containing that bounded/redacted
context, immutable capability matrix, `plan_version`, and budget limits. It
never contains raw API keys, raw provider responses, unauthorized tool names,
or an unbounded attachment body.

- [ ] **Step 4: Implement Coordinator validation and assignment**

Call `router.complete(ModelStage.DECOMPOSE, request)` exactly once. Redact the
returned mapping, validate `DecompositionDocument`, require the returned plan
version to equal the turn plan version, enforce the turn's `max_subtasks`, and
then call `AssignmentPolicy.assign`. Return a copy of `ModelResponse` whose
`data` is the same redacted mapping passed to validation, never the raw model
mapping. Return:

```python
class CoordinatorResult(StrictModel):
    document: DecompositionDocument
    assignments: list[AssignmentDecision]
    model_response: ModelResponse
```

Do not persist, emit events, enqueue, call tools, or catch `ProviderFailure` in this task.

- [ ] **Step 5: Add explicit deterministic Mock decomposition**

When `request.response_schema["title"] == "DecompositionDocument"`,
`MockProvider` returns a stable dependency-valid document that, when
`max_subtasks >= 2`, assigns at least one Chinese/material task to GLM and one
code/security task to DeepSeek. When the budget is exactly one, it returns one
valid required task. It always respects `plan_version` and `max_subtasks` and
uses only payload-authorized tools. Its `ModelResponse` must be
`provider="mock"`, `model="deterministic-mock"`,
`emulated_provider="deepseek"`, `emulated_model="deepseek-v4-flash"`,
`is_demo=True`. Legacy mock schemas keep their current output.

- [ ] **Step 6: Align configured defaults without weakening route enforcement**

Change the application, `.env.example`, and both Docker service defaults from `deepseek-v4-pro` to `deepseek-v4-flash`, updating exact configuration tests. Keep administrator-selected ProviderRoute models visible, but Coordinator routing must reject a non-Flash configured model rather than silently substitute or fall back.

- [ ] **Step 7: Run complete verification and commit**

Run all new decomposition/assignment/coordinator/router/provider/mock/config focused tests, then the full backend suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests
git diff --check
```

Self-review against the global constraints, confirm legacy task tests remain green, write the exact RED/GREEN and full-suite evidence to the SDD report, and commit:

```powershell
git add backend .env.example docker-compose.yml
git commit -m "feat: add DeepSeek decomposition coordinator"
```
