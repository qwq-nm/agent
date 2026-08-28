# Baseline frontend test stabilization

## Scope

Repair the pre-existing frontend test drift on the `feature/conversational-multi-agent` worktree. Do not change production code.

## Required changes

1. In `frontend/tests/provider-credentials.spec.ts` and `frontend/tests/admin-views.spec.ts`, make the hoisted API fakes implement the existing `SystemView` API contract:
   - `getDeepseekRoute`
   - `listDeepseekRouteOptions`
   - add `saveDeepseekRoute` only if needed by an existing test
   Configure safe default resolved values in each `beforeEach` so `SystemView.load()` reaches all existing mocks.
2. In `frontend/tests/task-create.spec.ts`, interact with the current advanced-settings authorization input instead of the removed always-visible `data-test="authorization"` field. Preserve assertions for the create payload, plan request, and navigation.
3. In `frontend/tests/team-task-detail.spec.ts`, assert the current localized token metric (`Token 用量` and `12 / 8`) instead of the stale `12 / 8 tokens` string.
4. Do not weaken behavior assertions, hide unhandled rejections, or change production components to accommodate incomplete mocks.

## Verification

- Run the four affected spec files together and record the exact result.
- Run `npm run test -- --run` in `frontend` and record the exact result.
- Run `npm run build` in `frontend` and record the exact result.
- Run `git diff --check`.

## Delivery

Commit the test-only changes with a focused commit. Write the full report to `.superpowers/sdd/baseline-frontend-report.md` with files changed, diagnosis, commands, exact test/build outcomes, commit hash, and self-review. Return only status, commit, one-line verification summary, and concerns.
