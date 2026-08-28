# Conversation service and attachment storage checkpoint

Implement the orchestration boundary below on top of the reviewed
`ConversationRepository` at `53aecc94`. This checkpoint is service/domain/config
and storage only. Do not add HTTP routes, SSE ticket code, DAG/Subtask/Worker,
provider/model calls, queue jobs, stop/failure decisions, or frontend changes.

## Files and boundaries

Expected production surface (names may vary only when an existing convention
requires it):

- extend `backend/secagent/conversation_domain.py` with strict response/request
  DTOs needed by the service;
- extend `backend/secagent/config.py` with the server-owned conversation limits;
- add `backend/secagent/services/conversation_events.py`;
- add `backend/secagent/services/conversation_storage.py`;
- add `backend/secagent/services/conversation_service.py`;
- add only the smallest owner-aware idempotency lookup needed to
  `backend/secagent/conversation_repository.py`;
- focused tests under `backend/tests/unit/`.

`ConversationService` owns the request-scoped writer `Session`. Its
`ConversationRepository`, `TaskRepository`, typed event writer, and audit
writer must all expose and share that exact same session object; reject a
miswired service before any file operation or SQL. The service also receives a
separate `session_factory` used only for short-lived read/preflight sessions.

Every public service write method requires a clean writer session at entry:
`session.in_transaction()` is false and `session.new`, `session.dirty`, and
`session.deleted` are all empty. Otherwise raise
`ConversationRepositoryStateError` before file work or SQL. Authorization and
idempotency preflight run in a separate short read transaction which is closed
before staging; staging must never overlap a database transaction. The service
then opens the only root write transaction. Every repository, Task, event, and
audit mutation inside it uses `commit=False` or flush-only behavior, and only
the root transaction commits. These service writers must not be invoked inside
an external transaction.

## Strict DTO contract

Add strict `extra="forbid"` DTOs using the existing `_StrictModel` style:

- a message submission containing `content` and a list of optional safe client
  relative paths. Content is 1..64,000 characters and cannot be whitespace-only;
  relative-path count is capped later by server settings and each non-null path
  uses `SafeClientRelativePath`;
- a message-with-attachments item;
- `ConversationDetailRead` containing the conversation, an ordered page of
  message items, ordered turns, and the active Turn if present;
- `MessageSendRead` containing the refreshed user message, its attachments,
  its Turn, and `replayed: bool`.

All returned objects are DTOs, never ORM rows or raw JSON.

The required idempotency key is a strict non-empty, non-whitespace string of at
most 255 characters; do not silently trim or truncate it.

## Server configuration

Add these exact defaults to `Settings` (environment names follow normal
BaseSettings uppercase mapping):

- `max_parallel_subtasks_per_conversation = 3`
- `max_subtasks_per_turn = 12`
- `max_model_calls_per_subtask = 4`
- `max_tool_calls_per_subtask = 6`
- `subtask_timeout_seconds = 180`
- `max_replans_per_turn = 2`
- `max_conversation_context_tokens = 32000`
- `max_attachments_per_message = 20`
- `max_attachment_total_bytes = 209715200`

Reject booleans, zero/negative values, and invalid parallelism above 3. Keep
legacy task settings unchanged.

Each new Turn snapshots those six Turn budget values. Requested conversation
parallelism does not override the global maximum. Each new legacy Task retains
the existing task budget configuration from Settings.

## Conversation event service

Expose a typed `append_message_created(...)` boundary rather than an arbitrary
caller-supplied event/payload persistence API. Its payload whitelist is exactly
conversation ID, message ID, Turn ID, legacy Task ID, sequence, plan version,
and attachment count. It emits `conversation.message.created`, composes with
`commit=False`, relies on the repository's per-conversation write lock, and
returns the structured event DTO.

Provide a pure encoder/validator utility used by that typed boundary and its
tests. It recursively redacts through the existing redaction utility, then
canonical-JSON encodes the redacted payload. An encoded payload of exactly
16,384 UTF-8 bytes is accepted; 16,385 bytes is rejected before the repository
is called. Neither this boundary nor any generic helper may persist arbitrary
caller fields. User content, filenames, API keys, raw prompts, raw model
responses, and hidden reasoning must never enter the event.

## Attachment staging and publishing

Implement an async `ConversationStorageService` under `Settings.data_dir`.
Uploads are never executed or imported.

1. `stage_many(uploads, relative_paths)` writes to a unique controlled staging
   directory, streams in bounded chunks, computes SHA-256, and returns an opaque
   staged batch owned by the service.
2. Validate before/during staging:
   - upload count <= `max_attachments_per_message`;
   - relative path count equals upload count;
   - each file <= existing `upload_max_bytes`;
   - aggregate raw bytes <= `max_attachment_total_bytes`;
   - a filename is required, first normalized to NFC, then validated as a safe
     single path segment of <=255 characters,
     with no traversal, separators, Windows device/ADS alias, control character,
     trailing dot/space, or absolute/drive path;
   - relative paths already satisfy `SafeClientRelativePath`;
   - server derives content type from the safe filename (fallback
     `application/octet-stream`) and never trusts the multipart content type.
3. ZIP files are preserved and inspected/extracted read-only in staging via the
   existing safe ZIP extraction path and existing archive file/byte limits.
   Detect ZIP input from the normalized name and/or signature before assigning
   its opaque stored name. Give each archive its own extraction directory. Each
   archive entry must independently pass strict relative-path and per-segment
   length checks, control-character rejection, Windows device/ADS and trailing
   dot/space rejection, and NFC-normalized duplicate plus case-fold collision
   rejection. Traversal, symlink, collision, or archive-limit failure rejects
   the entire batch.
4. `publish(batch, conversation_id, message_id)` atomically moves the staged
   workspace to a controlled
   `conversations/<conversation>/messages/<message>/...` directory and returns
   validated `AttachmentMetadataCreate` values. Each `storage_ref` is a POSIX
   relative path under `data_dir`; stored names are server-generated and do not
   reuse the client filename or its suffix. Content type is derived from the
   normalized client filename before the opaque suffix-free storage name is
   assigned.
5. Provide idempotent, path-confined cleanup for a staged batch and for one
   published message workspace. Cleanup accepts only a service-issued staged
   batch or a canonical UUID conversation/message pair whose resolved target is
   at the exact expected depth under `data_dir`. Refuse an unresolved or
   caller-provided path, filesystem root, data root, conversation root,
   `messages` parent, symlink, or junction.

An empty upload list produces an empty staged batch without creating a
directory. Any partial staging/publish failure cleans its own files.

## Conversation service CRUD and detail

Implement create/list/get-detail/patch/archive using the authenticated actor.
Writes use the clean-root transaction contract above and append a redacted
audit record in that same sole root transaction. Use the exact actions
`conversation.create`, `conversation.patch`, `conversation.archive`, and
`conversation.message.send`, with `resource_type="conversation"`,
`outcome="success"`, and details containing IDs/counts only. Archive is soft:
history stays readable; new messages are rejected.

Detail accepts repository-supported message/Turn pagination arguments and
returns:

- the authorized conversation;
- messages in sequence order, each with only its own attachments;
- Turns in plan-version order;
- active Turn resolved from `active_turn_id`.

Missing resources remain not-found; forbidden access uses the existing
`ForbiddenResource("conversation")` contract.

## Send-message transaction

Expose one async send operation with trusted actor, conversation ID, strict
submission DTO, uploads, and a required validated idempotency key.

First use the separate short-lived preflight session to authorize the actor and
look up conversation + idempotency key, then close that transaction. An absent
key on an archived conversation is rejected before staging. An existing key
with different text conflicts immediately. After preflight closes, stage/hash
uploads so a slow upload holds no DB transaction. Staging is temporary and must
be cleaned on every exit. The request-scoped writer session must still satisfy
the clean-entry contract before staging begins and before its root write
transaction is opened.

For a new active-conversation message, the one service-owned root write
transaction must:

1. append a completed `user/user_text` message using the supplied
   idempotency key;
2. publish the staged files, then insert every attachment metadata row;
3. create one legacy `TaskRow` owned by `Conversation.owner_id` (an acting admin
   appears only in audit actor fields), configure the existing Task budgets,
   and create one Conversation Turn linked to both Task and trigger message
   with the current immutable Turn budget snapshot;
4. link the user message to the Turn (through repository Turn creation);
5. if and only if this is sequence 1 and the current title is exactly the
   default `新对话`, patch the title to collapsed-whitespace user content,
   truncated to 40 Unicode characters; title generation never calls a model;
6. append `conversation.message.created` with IDs, sequence, plan version and
   attachment count only (never content or filenames);
7. append a success audit record without message content or filenames;
8. commit exactly once, then return refreshed `MessageSendRead` without opening
   a second write transaction.

Snapshot the six Turn budget fields exactly once while the conversation lock is
held. Build the compatibility Task goal deterministically as
`("Conversation turn: " + re.sub(r"\s+", " ", content).strip())[:4000]`, so
its existing 3..4000 constraint always holds. Preserve the conversation
authorization scope only when its length is 3..2000; otherwise use exactly
`No external target authorization granted.`. Preserve its safety mode. Copy the
four existing Task budget fields from Settings unchanged. Do not publish a Job
or call a provider in this checkpoint. New Task and Turn status remain their
schema defaults, ready for the later decomposition slice.

The first-title rule is exact:
`re.sub(r"\s+", " ", content).strip()[:40]`, and applies only when the new
message sequence is 1 and the locked current title is exactly `新对话`.

For an exception before `commit()` is called, or a commit failure for which the
database explicitly reports that the transaction did not commit, roll back the
service-owned root transaction and idempotently remove both any published
message workspace and staging. If `commit()` raises and its outcome is
uncertain, do **not** delete the published workspace: close/invalidate that
Session, surface a reconciliation-required failure, and leave resolution to the
future recovery protocol. Only an explicit successful return from `commit()`
enters committed state; after that, response refresh or serialization failures
must not delete files and the client recovers by replaying the same idempotency
key. Process termination and uncertain COMMIT outcomes are outside this
checkpoint's synchronous compensation guarantee. Crash orphan reconciliation
requires a future persistent publish-intent/recovery protocol; do not claim
global DB/filesystem atomicity here.

## Idempotent replay

Add the smallest repository lookup that finds one message by conversation +
idempotency key after authorizing the actor. It returns a DTO or `None` and does
not mutate/commit.

Replay behavior is exact:

- same key + same message content + the same attachment multiset returns the
  original refreshed message, attachments, and Turn with `replayed=True`;
- attachment identity is the multiset of `(original_name, relative_path,
  server-derived content_type, size_bytes, sha256)`; `storage_ref`, random stored
  name, DB ID, timestamp, and list order are not identity;
- a different message identity uses the existing dedicated message
  idempotency conflict; a different attachment multiset raises a dedicated
  conflict mapped later to HTTP 409;
- replay creates no file workspace, attachment, Task, Turn, event, audit, or
  Job and advances no counter;
- replay remains available after the conversation is archived, while a new key
  on an archived conversation is rejected before staging/publishing permanent
  files;
- a replay whose persisted message lacks its Turn is corrupt state and fails
  closed; never silently repairs it in this checkpoint.

The attachment comparison tuple is exact and multiplicity-sensitive:
`Counter[(original_name, relative_path, content_type, size_bytes, sha256)]`.
List order is irrelevant, duplicate count is not. Replays emit no audit record.

Race and archive linearization are exact. After staging, the writer transaction
calls the repository `add_message(..., commit=False)` path that takes the
conversation lock and rechecks current state. If it reports `created=False`,
reread the winning message, attachments, and Turn after that lock under
PostgreSQL READ COMMITTED/SQLite committed visibility, compare the exact
attachment counter, delete loser staging, and return replay. If it reports
`created=True`, reread the locked conversation status before publish; if it is
now archived, roll back and reject. Two simultaneous same-key requests may both
stage, but only the winner composes rows/files. Never handle the unique race by
calling `session.rollback()` inside a caller transaction.

## TDD and verification

Write tests first and record meaningful RED. At minimum cover:

1. strict new DTOs and all new Settings defaults/boundaries/bool rejection;
2. staging/publishing of multiple files, safe relative paths, server content
   type, hashes, ZIP safety, all count/per-file/aggregate/path limits, and
   idempotent confined cleanup;
3. CRUD/detail ordering, owner/admin RBAC, archive history, and no hard delete;
4. one text message creates exactly one message/Task/Turn/event/audit, links all
   IDs, snapshots budgets, derives first title, and invokes no queue/provider;
5. two rounds preserve first title and show settings changes affect only the
   later Turn snapshot;
6. multiple attachments persist accurate metadata and controlled files;
7. sequential and concurrent same-key replay returns original objects and
   leaves exact row/file/counter counts; changed text or attachment multiset
   conflicts;
8. concurrent replay is exercised with file-backed SQLite and with a real
   PostgreSQL READ COMMITTED barrier test; cover duplicate attachment
   multiplicity and order independence. The PostgreSQL test may skip during an
   ordinary local run, but `DONE` acceptance requires recorded passing evidence
   from a real PostgreSQL run. If that environment cannot be provided, return
   `NEEDS_CONTEXT` or `BLOCKED`; SQLite evidence is not a substitute;
9. archived exact replay succeeds, archived new key fails, and an archive/send
   interleave is linearized correctly;
10. injected failures before and after publish roll back every listed DB
    mutation and remove files, while an injected response failure after a
    successful commit retains committed files for replay;
11. short/whitespace-collapsed compatibility goals and invalid/empty scopes use
    the exact safe fallback; Task owner remains conversation owner for admin
    sends;
12. ZIP Windows aliases, ADS, trailing-dot/space, NFC duplicate and case-fold
    collisions fail closed; cleanup refuses roots and symlink/junction targets;
13. event deep redaction, whitelist enforcement, exact 16,384-byte acceptance,
    and 16,385-byte rejection before any partial event.

Run the focused tests, then the full backend suite with
`PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. Write the detailed report
to `.superpowers/sdd/conversation-service-storage-report.md`, run
`git diff --check`, commit, and return DONE/NEEDS_CONTEXT/BLOCKED with commit and
test evidence. Do not spawn subagents.
