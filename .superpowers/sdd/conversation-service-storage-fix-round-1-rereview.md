# Conversation service/storage fix round 1 scoped re-review

## Scope and method

This is a scoped re-review of only the three findings from
`conversation-service-storage-review-findings.md` and the fix-only package
`5eebdccd..2fc0fa0c`. It is not a new full review.

I read the relevant checkpoint contract, the complete original finding report,
the complete fix report (including the controller live-PostgreSQL addendum), and
the complete fix-only review package before inspecting only the necessary
current-file context for the changed commit, ZIP/path, and issued-batch paths.
The supplied evidence answered the concrete questions, so I did not rerun a
diff, targeted test, focused suite, or full suite.

## Finding verdicts

### C1 Critical — ADDRESSED

The fix removes `Session.in_transaction()` from COMMIT outcome classification.
`ConversationCommitNotApplied` is the sole narrow typed signal accepted as
evidence that COMMIT was not attempted
(`backend/secagent/services/conversation_service.py:70-71`,
`backend/secagent/services/conversation_service.py:591-592`). Every other
exception from either send-message COMMIT attempt takes the uncertain path:
the main new-message path sets `preserve_published`, invalidates the writer
Session, and raises `ConversationCommitOutcomeUnknown`
(`backend/secagent/services/conversation_service.py:460-470`); the replay-race
COMMIT helper likewise invalidates and raises the reconciliation-required error
(`backend/secagent/services/conversation_service.py:579-589`). The outer error
handler removes a published workspace only when `preserve_published` is false,
so the uncertain path retains it (`backend/secagent/services/conversation_service.py:472-483`).

The explicit-not-applied regression now injects the typed pre-COMMIT signal and
checks database rollback plus workspace compensation
(`backend/tests/unit/test_conversation_service.py:730-757`). Separate coverage
checks a successful COMMIT followed by an exception and retention of the
committed workspace (`backend/tests/unit/test_conversation_service.py:759-778`),
an invalidated driver error (`backend/tests/unit/test_conversation_service.py:782-809`),
and, critically, a non-invalidated SQLAlchemy `OperationalError` while the
client Session still reports an active transaction. That test asserts exactly
one invalidation, closed transaction state, reconciliation-required failure,
and retained workspace (`backend/tests/unit/test_conversation_service.py:812-853`).

No C1 regression is introduced by the fix diff.

### I1 Important — ADDRESSED

The shared relative-path validator now rejects every character whose Unicode
category begins with `C` (`backend/secagent/conversation_domain.py:47-48`,
`backend/secagent/conversation_domain.py:282-304`). Both `SafeStorageRef` and
`SafeClientRelativePath` use that validator
(`backend/secagent/conversation_domain.py:307-315`). ZIP logical entry names
are normalized to NFC before `SafeClientRelativePath` validation, segment
length checking, and normalized case-fold collision checking
(`backend/secagent/services/conversation_storage.py:282-312`). Top-level
filenames are also normalized to NFC and use the same shared Unicode-category
policy (`backend/secagent/services/conversation_storage.py:317-333`).

The U+0085 top-level case fails closed and asserts no staging residue
(`backend/tests/unit/test_conversation_storage.py:146-173`). The ZIP U+0085
case goes through the whole archive batch rejection test, which likewise
asserts staging cleanup (`backend/tests/unit/test_conversation_storage.py:210-232`).
The staging implementation catches all failure/cancellation paths and removes
the batch workspace (`backend/secagent/services/conversation_storage.py:125-186`).
Existing valid NFC filename metadata and valid multi-archive paths remain
covered (`backend/tests/unit/test_conversation_storage.py:176-207`), and the
recorded focused/full suites supply broader non-regression evidence.

No I1 regression is introduced by the fix diff.

### M1 Minor — ADDRESSED

`StagedAttachmentBatch` is now weak-referenceable and the issued registry is a
`WeakKeyDictionary` (`backend/secagent/services/conversation_storage.py:67-80`,
`backend/secagent/services/conversation_storage.py:83-94`). Its values contain
only the batch ID or consumed `None`, so the registry does not retain a strong
path back to the batch or its `_entries` tuple. While the caller still holds a
batch, publish and cleanup retain the consumed `None` state, preserving
repeated-cleanup idempotency (`backend/secagent/services/conversation_storage.py:188-244`,
`backend/secagent/services/conversation_storage.py:252-263`).

Capability checks remain identity-based: a batch must be the right class, carry
the service-private owner object, be an extant key in that service's weak
registry, and have a batch ID identical to the registry state
(`backend/secagent/services/conversation_storage.py:358-374`). Because
`StagedAttachmentBatch` does not override equality or hashing, weak-key lookup
continues to use object identity; a separately constructed object cannot become
an issued capability merely by copying visible state.

The lifecycle regression exercises ten completed empty batches and one
published batch, performs repeated cleanup, releases all caller references,
forces collection, and verifies both that every weak reference is dead and the
registry is empty (`backend/tests/unit/test_conversation_storage.py:355-377`).

No M1 regression or new WeakKey/identity lifecycle defect is introduced by the
fix diff.

## Verification evidence reconciliation

The fix report and fix diff are mutually consistent:

- Targeted C1/I1/M1 run: `29 passed` (`conversation-service-storage-fix-round-1-report.md:99-110`). The six selected test functions in the diff expand to 29 cases through the filename and ZIP parametrizations.
- Non-PostgreSQL focused checkpoint run: `104 passed, 1 skipped` (`conversation-service-storage-fix-round-1-report.md:112-120`).
- Non-PostgreSQL full backend run: `413 passed, 1 skipped` (`conversation-service-storage-fix-round-1-report.md:132-139`).
- At committed HEAD `2fc0fa0c`, the controller's real PostgreSQL 16 isolated barrier: `1 passed` in `1.17s` (`conversation-service-storage-fix-round-1-report.md:149-158`). The barrier test exists at `backend/tests/unit/test_conversation_service.py:997`.
- At the same committed HEAD and live PostgreSQL URL, the six-file focused checkpoint suite: `105 passed` in `12.70s` (`conversation-service-storage-fix-round-1-report.md:158-162`).

The earlier inherited `105 passed` result at report lines 80-82 is not needed
for acceptance; the controller addendum supplies the required fresh committed-
HEAD live-PostgreSQL evidence.

## New breakage in the fix diff

- Critical: 0
- Important: 0
- Minor: 0

## Out-of-scope observations

None. No unchanged behavior was elevated into a new review finding during this
scoped fix verification.

## Final verdict

All findings addressed, no new Critical/Important breakage
