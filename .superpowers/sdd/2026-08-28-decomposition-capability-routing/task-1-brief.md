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

