# Baseline frontend test stabilization report

## Scope and files changed

- `frontend/tests/provider-credentials.spec.ts`
- `frontend/tests/admin-views.spec.ts`
- `frontend/tests/task-create.spec.ts`
- `frontend/tests/team-task-detail.spec.ts`

No production files were modified.

## Diagnosis

`SystemView.load()` now calls `getDeepseekRoute()` and
`listDeepseekRouteOptions()` alongside its existing status APIs. The two
hoisted test fakes did not implement those methods, causing unhandled rejected
promises and leaving the view in its initial state. Both fakes now implement
the complete consumed contract and use safe, representative resolved route
data in `beforeEach`.

The task-create spec still selected the removed always-visible authorization
control. It now opens the current advanced settings panel and fills its
authorization-scope textarea, while asserting the create payload, plan request,
and navigation. The model-route metric spec now reflects the localized UI:
`Token 用量` and `12 / 8`.

## Verification

All commands were run from `frontend` unless noted.

| Command | Exact outcome |
| --- | --- |
| `npm run test -- --run tests/provider-credentials.spec.ts tests/admin-views.spec.ts tests/task-create.spec.ts tests/team-task-detail.spec.ts` | PASS — 4 test files, 18 tests passed. |
| `npm run test -- --run` | PASS — 9 test files, 41 tests passed. |
| `npm run build` | PASS — `vue-tsc --noEmit && vite build`; 77 modules transformed; build completed in 2.14s. |
| `git diff --check` (repository root) | PASS — no whitespace errors. |

## Self-review

- The only behavior changes are test expectations and API fake completeness.
- No assertions were weakened or hidden behind error handling.
- The task-create test continues to cover the request payload, plan endpoint,
  and destination navigation.
- The build and complete frontend test suite pass without unhandled rejections.

## Commit

`960d68e9325585d64e04795961afa9027124e74b` (`test: stabilize baseline frontend specs`)
