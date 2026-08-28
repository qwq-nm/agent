# SDD ledger — plan: docs/superpowers/plans/2026-08-28-decomposition-capability-routing.md

Spec authority: `docs/superpowers/specs/2026-08-26-conversational-multi-agent-workflow-design.md`.

## Pre-flight interface/conflict scan

| Scope | Producer / consumer | Finding | Resolution |
|---|---|---|---|
| Task 1 self-check | strict DTO bounds vs dynamic graph/budget validator | Consistent after requiring 1..64 subtasks and 1..32 synthesis requirements | Proceed |
| Task 2 self-check | matrix preferences vs deterministic assignment/correction output | Consistent; policy reasons are controlled backend codes and model output cannot claim `POLICY_*` codes | Proceed |
| Task 3 self-check | deterministic Mock must show both logical models vs `max_subtasks=1` | Conflict found | Ruling recorded below; plan corrected before implementation |
| Task 1 -> Task 2 | `DecompositionDocument` / capability enums consumed by assignment policy | Names and provider/reason enum values match | Proceed |
| Task 1 -> Task 3 | validated document and expected plan/budget checks consumed by Coordinator | Coordinator delegates all graph checks to Task 1 interface | Proceed |
| Task 2 -> Task 3 | `AssignmentPolicy.assign` and `AssignmentDecision` consumed by Coordinator | Signature and returned fields match | Proceed |
| Task 3 -> legacy agents/providers | new stages and dynamic `subtask_execute` route share `ModelStage`, providers, and router with historical task stages | Plan explicitly preserves legacy fixed stages and requires their regression suite | Proceed |
| Task 3 -> future DAG persistence | returns redacted validated document, controlled decisions, and sanitized model metadata without writes | Clean boundary; persistence remains next slice | Proceed |
| Task 3 provider availability | AssignmentPolicy must check configured providers; a caller-supplied set could lie about runtime configuration | Plan corrected so Coordinator derives availability from `ModelRouter`, with logical emulation only in explicit mock mode | Proceed |

Ruling: deterministic Mock dual-model graph under a one-subtask budget — when
`max_subtasks >= 2`, emit both GLM and DeepSeek tasks; when it equals one, emit
one valid required task. This preserves the hard budget while the default/E2E
budget still demonstrates both models. If wrong, a one-slot demo may fail to
show both model labels, but it will not exceed an administrator-controlled
budget.

Ruling: provider availability is derived from the runtime router rather than
accepted from a Coordinator caller — this makes “configured provider” a backend
fact. Explicit mock mode exposes both logical providers only as declared
emulation. If wrong, custom internal callers lose the ability to simulate an
unconfigured provider without constructing a matching router.

Task 1: complete at `d4cc3462`. Initial review found one Important boundary
gap (`plan_version >= 1`); fix round 1 addressed it. Scoped re-review:
Critical 0, Important 0, Minor 0, APPROVE. Final focused evidence: 31 passed
plus clean `git diff --check`.
Task 2: complete at `a54a761b`. Initial review found Critical 0, Important 2,
Minor 1; fix round 1 addressed the immutable route-text registry, consistent
correction state, and effective purity test. Scoped re-review: all addressed,
no new findings, APPROVE. Controller verification including conversation-domain
regressions: 47 passed plus one pre-existing deprecation warning.
Task 3: fix round 1 in progress after review found Critical 0, Important 2,
Minor 0: preserve validated numeric budget limits when redacting the outbound
request and reject non-string coercion for provider/risk enum inputs.

Ruling: the original Task 3 implementer completed I1/I2 RED→GREEN and the
221-test focused run, but could not resume its interrupted full-suite handoff
because the host rejected its previously accepted Sol model. Preserve its
two-file diff and use a fresh Terra validation agent only for verification,
reporting, and commit. This avoids controller-authored changes while keeping
the fix scope unchanged.
