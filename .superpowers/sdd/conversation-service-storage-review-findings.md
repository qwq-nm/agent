# Conversation service/storage checkpoint independent review

## Verdicts

- **Spec Compliance: ❌**
- **Task quality: Needs fixes**
- Findings: **Critical 1 / Important 1 / Minor 1**

Review scope was limited to the checkpoint described by
`conversation-service-storage-brief.md`. I read the brief, implementation
report, and the complete `53aecc94..5eebdccd` review package before checking
only the unchanged call sites needed to resolve concrete transaction and path
validation questions. I did not rerun the full suite. The recorded SQLite and
PostgreSQL results are consistent with the tests in the diff, but those tests
do not invalidate the findings below.

## Critical

### C1. COMMIT exceptions are treated as explicit non-commit without database evidence

**Evidence:** `backend/secagent/services/conversation_service.py:458`,
`backend/secagent/services/conversation_service.py:460`, and
`backend/secagent/services/conversation_service.py:588`-`591`.

`_commit_explicitly_not_applied()` returns `True` for every COMMIT exception
except a `DBAPIError` whose `connection_invalidated` flag is set, as long as
SQLAlchemy still exposes a transaction object. `Session.in_transaction()` is
client-side session state; it is not an explicit report from the database that
COMMIT was not applied. A timeout, protocol error, or driver error after COMMIT
was sent can leave that flag false and the Session transaction present even
though the server outcome is unknown.

**Impact:** the branch at lines 460-462 rolls back locally and then the outer
handler removes the published message workspace. If the server actually
committed, durable attachment rows remain while their files are deleted. This
is the exact cross-system corruption the brief's fail-closed uncertain-COMMIT
rule is intended to prevent.

The test at `backend/tests/unit/test_conversation_service.py:738` injects a
plain exception before calling the real COMMIT and therefore does not establish
an explicit database non-commit signal. The post-COMMIT test at line 764 only
covers the easier case where the real commit has already cleared SQLAlchemy's
transaction state.

**Fix:** make uncertainty the default for every exception raised by the COMMIT
attempt. Compensate files only for a narrow, explicit signal that proves the
transaction was aborted before COMMIT could take effect (for example, a typed
pre-COMMIT/explicit-abort outcome from a controlled transaction adapter), not
from `Session.in_transaction()`. Add a focused test where a non-invalidated
DBAPI/driver COMMIT error leaves `in_transaction()` true and verify that the
Session is invalidated, reconciliation-required is raised, and the workspace
is retained. Keep a separate test for the genuinely explicit non-commit case.

## Important

### I1. ZIP entry validation accepts Unicode control characters

**Evidence:** `backend/secagent/services/conversation_storage.py:290`-`303`
delegates an NFC-normalized ZIP entry to `SafeClientRelativePath`, while
`backend/secagent/conversation_domain.py:290`-`294` rejects only the existing
Windows-forbidden set and characters with code points below 32.

Unicode C1 controls such as U+0085 are category `Cc` but have code points above
31, so `SafeClientRelativePath` accepts a ZIP member such as
`bad\u0085.txt`. A focused validation check against the submitted code
confirmed that acceptance. `_inspect_archive()` performs no additional
Unicode-category control-character check before `extract_zip_safely()` writes
the entry.

**Impact:** an archive that the brief requires to fail closed is accepted and
extracts a control-character filename into the staged/published workspace.
Besides violating the exact ZIP contract, such names are ambiguous in logs,
terminal output, and downstream file tooling.

**Fix:** after NFC normalization and before collision tracking/extraction,
reject Unicode control characters in every segment (at minimum category `Cc`;
using the same `category(...).startswith("C")` policy as top-level filenames
would keep both boundaries consistent). Add ZIP cases for U+0085 and any other
chosen non-ASCII control boundary, and assert the whole batch is removed.

## Minor

### M1. The issued-batch capability registry retains every batch indefinitely

**Evidence:** `backend/secagent/services/conversation_storage.py:90` creates a
normal dictionary keyed by `StagedAttachmentBatch`. Successful publish,
publish failure, and cleanup only replace the value with `None` at lines
231, 239, and 258; no path removes the key. Each key strongly retains its
`_entries` tuple as well.

**Impact:** if `ConversationStorageService` is reused across requests, every
staging call, including empty batches and completed requests, permanently grows
the registry. Memory usage therefore scales with lifetime request/attachment
count rather than active staged work.

**Fix:** keep capability verification without permanent strong references—for
example, use weak keys (and make the batch weak-referenceable), or remove
consumed registry entries and represent the idempotent-cleaned state in an
unforgeable immutable capability/state design. Add a lifecycle test showing
that completed batches are collectible while repeated cleanup remains safe.

## Spec compliance summary

The diff otherwise implements the requested production boundary closely:
strict DTOs and configuration limits, a typed seven-field event writer,
request-scoped writer composition, separate preflight/staging, CRUD/detail and
RBAC/audits, owner-preserving admin sends, one legacy Task and Turn per new
message, budget/title/goal/scope rules, multiset replay, archive/send locking,
opaque file publication, ZIP collision/device/traversal checks, and no
HTTP/SSE/DAG/provider/queue/worker/frontend expansion. The SQLite and live
PostgreSQL tests exercise the intended same-key linearization.

Spec compliance is nevertheless ❌ because C1 violates the mandatory
uncertain-COMMIT compensation boundary and I1 violates the mandatory ZIP
control-character rejection. Task quality is **Needs fixes** until those two
behavioral defects are corrected; M1 should be corrected with them or tracked
explicitly as lifecycle debt.
