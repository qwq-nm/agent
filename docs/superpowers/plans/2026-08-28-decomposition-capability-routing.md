# DeepSeek Decomposition and Capability Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the strict decomposition contract, deterministic GLM/DeepSeek assignment policy, and fixed DeepSeek V4 Flash coordinator boundary used by future persistent DAG scheduling.

**Architecture:** Keep model-produced structure, backend policy, and provider invocation in three separate units. `conversation_decomposition.py` owns strict DTOs and graph invariants; `assignment_policy.py` owns immutable capability preferences and contextual corrections without calling a model; `coordinator.py` builds a bounded/redacted request, calls only the fixed `decompose` route, validates the result, and returns an in-memory coordinated plan for the next persistence slice.

**Tech Stack:** Python 3.12, Pydantic v2, pytest/pytest-asyncio, existing `ModelRouter`, DeepSeek/GLM/Mock providers, existing redaction and canonical JSON helpers.

**Spec:** `docs/superpowers/specs/2026-08-26-conversational-multi-agent-workflow-design.md`

## Global Constraints

- `decompose` is owned by provider `deepseek` with actual model identifier exactly `deepseek-v4-flash`; live/auto never falls back to GLM or Mock.
- Only explicit `MODEL_MODE=mock` may emulate decomposition, and its response must state `provider=mock`, `emulated_provider=deepseek`, `emulated_model=deepseek-v4-flash`, and `is_demo=true`.
- GLM is preferred for Chinese semantics, long-document summarization, classification, and field extraction. DeepSeek V4 Flash is preferred for code/security reasoning, reverse/causal analysis, evidence-conflict reasoning, decomposition, and synthesis.
- The capability matrix and all route-reason text are backend constants. Model output cannot modify them.
- Assignment policy is deterministic and never calls a provider. It validates provider availability, capabilities, dependency validity, authorized tools, and turn budget.
- Every model-facing and model-returned JSON value is strictly validated and redacted before a later persistence boundary can consume it. Raw prompts/responses and credentials are not events or audit details.
- Preserve legacy `task_parse`, `plan`, `critic`, and `report` behavior and wire contracts.
- This plan does not persist subtasks, modify the state machine, enqueue jobs, execute tools, synthesize answers, add HTTP routes, or change the frontend.

---

### Task 1: Strict decomposition document and DAG validation

**Files:**
- Create: `backend/secagent/conversation_decomposition.py`
- Create: `backend/tests/unit/test_conversation_decomposition.py`

**Interfaces:**
- Produces enums `Capability`, `LogicalProvider`, and `RouteReasonCode`.
- Produces strict DTOs `SubtaskSpec` and `DecompositionDocument`.
- Produces `validate_decomposition(document, *, expected_plan_version, max_subtasks) -> DecompositionDocument`.
- Later tasks consume the validated document without reimplementing key/dependency/cycle/budget checks.

- [ ] **Step 1: Write the failing happy-path and strict-schema tests**

```python
def valid_document() -> dict:
    return {
        "plan_version": 2,
        "goal_summary": "分析项目中的高风险问题",
        "subtasks": [
            {
                "key": "extract_context",
                "title": "提炼中文材料",
                "objective": "提取项目材料中的关键事实和约束。",
                "dependency_keys": [],
                "required_capabilities": ["chinese_semantic"],
                "proposed_provider": "glm",
                "route_reason_code": "glm_chinese_strength",
                "allowed_tools": [],
                "expected_output": "结构化事实摘要",
                "required": True,
            },
            {
                "key": "assess_risk",
                "title": "评估代码风险",
                "objective": "基于已提炼事实判断代码和安全风险。",
                "dependency_keys": ["extract_context"],
                "required_capabilities": ["code_security_reasoning"],
                "proposed_provider": "deepseek",
                "route_reason_code": "deepseek_code_security_strength",
                "allowed_tools": ["source_scanner"],
                "expected_output": "带依据的风险列表",
                "required": True,
            },
        ],
        "synthesis_requirements": ["区分事实和推断"],
    }


def test_decomposition_document_is_strict_and_validates_a_dag():
    document = DecompositionDocument.model_validate(valid_document())
    assert validate_decomposition(
        document, expected_plan_version=2, max_subtasks=2
    ) is document
    with pytest.raises(ValidationError):
        DecompositionDocument.model_validate(valid_document() | {"extra": True})
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests/unit/test_conversation_decomposition.py -q
```

Expected: collection/import failure because `secagent.conversation_decomposition` does not exist.

- [ ] **Step 3: Implement the strict enums and DTO bounds**

Implement `ConfigDict(extra="forbid", strict=True)` models and these exact enum values:

```python
class Capability(StrEnum):
    CHINESE_SEMANTIC = "chinese_semantic"
    LONG_DOCUMENT_SUMMARIZATION = "long_document_summarization"
    CLASSIFICATION_EXTRACTION = "classification_extraction"
    TASK_DECOMPOSITION = "task_decomposition"
    CODE_SECURITY_REASONING = "code_security_reasoning"
    REVERSE_CAUSAL_ANALYSIS = "reverse_causal_analysis"
    EVIDENCE_CONFLICT_RESOLUTION = "evidence_conflict_resolution"
    FINAL_SYNTHESIS = "final_synthesis"
    TOOL_REQUEST = "tool_request"


class LogicalProvider(StrEnum):
    GLM = "glm"
    DEEPSEEK = "deepseek"


class RouteReasonCode(StrEnum):
    GLM_CHINESE_STRENGTH = "glm_chinese_strength"
    GLM_LONG_DOCUMENT_STRENGTH = "glm_long_document_strength"
    GLM_EXTRACTION_STRENGTH = "glm_extraction_strength"
    DEEPSEEK_DECOMPOSITION_FIXED = "deepseek_decomposition_fixed"
    DEEPSEEK_CODE_SECURITY_STRENGTH = "deepseek_code_security_strength"
    DEEPSEEK_REVERSE_CAUSAL_STRENGTH = "deepseek_reverse_causal_strength"
    DEEPSEEK_EVIDENCE_CONFLICT_STRENGTH = "deepseek_evidence_conflict_strength"
    DEEPSEEK_SYNTHESIS_FIXED = "deepseek_synthesis_fixed"
    BALANCED_MODEL_SUGGESTION = "balanced_model_suggestion"
    TOOL_COMPATIBLE = "tool_compatible"
    POLICY_PREFERRED_CAPABILITY = "policy_preferred_capability"
    POLICY_PROVIDER_UNAVAILABLE = "policy_provider_unavailable"
    POLICY_CAPABILITY_MISMATCH = "policy_capability_mismatch"
    POLICY_TOOL_FILTERED = "policy_tool_filtered"
```

Use these bounds: key pattern `^[a-z][a-z0-9_-]{0,63}$`; title 1..80; objective 1..2,000; goal summary 1..2,000; expected output 1..1,000; each synthesis requirement 1..1,000; at most 64 dependency keys, 9 unique capabilities, 64 unique tools, 1..64 subtasks, and 1..32 synthesis requirements. Reject whitespace-only strings, duplicate dependencies/capabilities/tools, and self-dependencies.
`SubtaskSpec.route_reason_code` accepts only the ten non-`POLICY_*` codes;
policy correction codes are backend output and a model cannot claim that a
correction already occurred.

- [ ] **Step 4: Add graph and dynamic-budget tests, then implement validation**

Add parameterized tests that independently catch duplicate keys, missing dependencies, self-dependency, a multi-node cycle, wrong plan version, `max_subtasks < 1`, and document size above the supplied limit. Implement deterministic DFS/Kahn validation that reports only safe key/reason text and returns the original document on success.

- [ ] **Step 5: Run focused tests and commit**

Run the Task 1 file, then:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider backend/tests/unit/test_conversation_domain.py backend/tests/unit/test_conversation_decomposition.py -q
git diff --check
git add backend/secagent/conversation_decomposition.py backend/tests/unit/test_conversation_decomposition.py
git commit -m "feat: add strict decomposition contract"
```

### Task 2: Deterministic capability matrix and assignment policy

**Files:**
- Create: `backend/secagent/services/assignment_policy.py`
- Create: `backend/tests/unit/test_assignment_policy.py`

**Interfaces:**
- Consumes: `DecompositionDocument`, `SubtaskSpec`, `Capability`, `LogicalProvider`, `RouteReasonCode` from Task 1.
- Produces immutable `CAPABILITY_MATRIX`, safe `ROUTE_REASON_TEXT_ZH`, strict `AssignmentDecision`, `AssignmentPolicyError`, and `AssignmentPolicy.assign(...) -> list[AssignmentDecision]`.
- Task 3 consumes decisions containing `key`, `proposed_provider`, `assigned_provider`, `route_reason_code`, `route_reason`, effective `allowed_tools`, `corrected`, and controlled `correction_codes`.

- [ ] **Step 1: Write the failing matrix/preference tests**

```python
def test_policy_corrects_pure_chinese_and_code_tasks_to_provider_strengths():
    decisions = AssignmentPolicy().assign(
        document_with(
            subtask("cn", [Capability.CHINESE_SEMANTIC], "deepseek"),
            subtask("code", [Capability.CODE_SECURITY_REASONING], "glm"),
        ),
        available_providers={"glm", "deepseek"},
        registered_tools=set(),
        authorized_tools=set(),
    )
    assert [(item.key, item.assigned_provider) for item in decisions] == [
        ("cn", LogicalProvider.GLM),
        ("code", LogicalProvider.DEEPSEEK),
    ]
    assert all(item.corrected for item in decisions)
```

Also test that Task Decomposition and Final Synthesis are DeepSeek-only; Tool Request is supported by both; mixed equally preferred capabilities preserve a valid model proposal; and matrix constants cannot be mutated through a returned view.

- [ ] **Step 2: Run the Task 2 test file and verify RED**

Expected: import failure because `secagent.services.assignment_policy` does not exist.

- [ ] **Step 3: Implement the immutable matrix and deterministic selection**

Use `CapabilityLevel = StrEnum("CapabilityLevel", {"UNSUPPORTED": "unsupported", "SUPPORTED": "supported", "PREFERRED": "preferred", "FIXED": "fixed"})` and an immutable mapping with these exact relationships:

```text
GLM: chinese/long-document/classification=PREFERRED;
     code-security/evidence-conflict/tool-request=SUPPORTED;
     task-decomposition/reverse-causal/final-synthesis=UNSUPPORTED.
DeepSeek: task-decomposition/final-synthesis=FIXED;
          code-security/reverse-causal/evidence-conflict=PREFERRED;
          chinese/long-document/classification/tool-request=SUPPORTED.
```

Candidate selection is deterministic: discard unavailable or unsupported providers; prefer the candidate with the most PREFERRED/FIXED matches; on an equal score preserve the valid proposed provider; if it is not a candidate use provider name order `deepseek`, then `glm`. Never choose `mock` as a logical assignment. If no live logical provider can satisfy all required capabilities, raise a safe `AssignmentPolicyError` containing only the subtask key and a controlled code.

- [ ] **Step 4: Add contextual provider/tool correction tests and implement them**

Test provider-unavailable correction, total provider failure, unknown providers, registered-but-unauthorized tools, authorized-but-unregistered tools, preserved tool order, and no mutation of the input document. Effective tools are the ordered intersection of model-requested, registered, and authorized tools. A filtered tool adds `POLICY_TOOL_FILTERED` to `correction_codes`; provider changes add the corresponding controlled provider correction. `route_reason` must come only from `ROUTE_REASON_TEXT_ZH`, never model prose.

- [ ] **Step 5: Verify the policy is pure and commit**

Run Task 1+2 focused tests. Add a spy/fake that would fail if any provider/model method is called and prove `AssignmentPolicy.assign` never touches it. Run `git diff --check`, then commit:

```powershell
git add backend/secagent/services/assignment_policy.py backend/tests/unit/test_assignment_policy.py
git commit -m "feat: add deterministic model assignment policy"
```

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
