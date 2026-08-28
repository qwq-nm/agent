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

