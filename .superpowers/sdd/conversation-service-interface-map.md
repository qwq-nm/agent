# Conversation service / attachment-storage interface map

Read-only reconnaissance completed 2026-08-27.  This maps the interfaces needed by `conversation-service-storage-brief.md`; it makes no design changes.

## Reusable boundaries

### Conversation repository

`backend/secagent/conversation_repository.py`

* `ConversationRepository(session: sqlalchemy.orm.Session)`.  Public `commit()` and `rollback()` delegate to that exact session.
* `create_conversation(actor, payload: ConversationCreate, *, commit: bool = True) -> ConversationRead`
* `get_conversation(actor, conversation_id: str) -> ConversationRead | None`; `list_conversations(actor, *, limit: int = 100) -> list[ConversationRead]`; `patch_conversation(actor, conversation_id, payload: ConversationPatch, *, commit=True) -> ConversationRead`; `archive_conversation(actor, conversation_id, *, commit=True) -> ConversationRead`.
* `add_message(actor, conversation_id, payload: ConversationMessageWrite, *, commit=True) -> MessageAppendResult`, where `MessageAppendResult(message: ConversationMessageRead, created: bool)`.  It locks the owning conversation first, allocates a per-conversation sequence atomically, and treats same `(conversation_id, idempotency_key)` + identical `(role, kind, content)` as replay.  A different immutable identity raises `MessageIdempotencyConflict`.
* `get_message(actor, conversation_id, message_id) -> ConversationMessageRead | None`; `list_messages(actor, conversation_id, *, after_sequence=0, limit=100) -> list[ConversationMessageRead]`.
* `add_attachment(actor, conversation_id, message_id, payload: AttachmentMetadataCreate, *, commit=True) -> AttachmentRead`; `list_attachments(actor, conversation_id, message_id, *, limit=100) -> list[AttachmentRead]`.
* `create_turn(actor, conversation_id, payload: ConversationTurnCreate, *, commit=True) -> ConversationTurnRead`; it requires the trigger message and optional replan turn to belong to the conversation, verifies a linked Task belongs to the conversation owner, links the trigger message, and makes the new highest plan version active.  Get/list and active-turn methods are also present.
* `append_event(actor, conversation_id, payload: ConversationEventCreate, *, commit=True) -> ConversationEventRead`; `events_after(actor, conversation_id, *, cursor=0, limit=100) -> list[ConversationEventRead]`.

Every repository write calls `_write`: it refuses meaningful pending caller state (`new`, `deleted`, or dirty), opens a savepoint, flushes, and commits only when `commit=True`.  It uses an ownership-filtered `UPDATE ... RETURNING` lock rather than `SELECT FOR UPDATE`.  Reusable exceptions: `ConversationInvariantError`, `ConversationRepositoryStateError`, `MessageIdempotencyConflict`; access is `KeyError` for absent resource and existing `ForbiddenResource("conversation")` for unauthorized access.

### Conversation DTOs and rows

`backend/secagent/conversation_domain.py`

* Strict (`extra="forbid"`) existing DTOs: `ConversationSettings`, `ConversationCreate`, `ConversationPatch`, `ConversationRead`, `UserMessageCreate`, `ConversationMessageWrite`, `ConversationMessageRead`, `TurnBudgetSnapshot`, `ConversationTurnCreate`, `ConversationTurnRead`, `AttachmentMetadataCreate`, `AttachmentRead`, `ConversationEventCreate`, and `ConversationEventRead`.
* `ConversationSettings` already provides `authorization_scope`, `safety_mode`, `allowed_targets`, and optional `requested_parallelism` (strict 1..3); it does not enforce a global server maximum.
* `AttachmentMetadataCreate(original_name, storage_ref, relative_path, content_type, size_bytes, sha256, status=ready)` supplies the repository input.  `storage_ref` and `relative_path` must be safe relative POSIX paths; SHA-256 must be lower-case 64 hex characters.
* `canonical_json_dumps(value)` / `canonical_json_loads(raw, target)` validate finite JSON data and canonicalize persisted JSON.

`backend/secagent/db_models.py`

* Conversation tables/rows are already registered in `Base`: `ConversationRow`, `ConversationMessageRow`, `MessageAttachmentRow`, `ConversationTurnRow`, `ConversationEventRow`.
* Constraints useful to the service: message `(conversation_id, sequence)` and `(conversation_id, idempotency_key)` are unique; turn `(conversation_id, plan_version)` is unique; event ID is `BigInteger` / SQLite `Integer` autoincrement; task FK on a turn is `SET NULL`.  Deleting a message/conversation cascades children, while archive is only a status update.

### Legacy Task compatibility

`backend/secagent/domain.py`: `TaskCreate(goal, authorization_scope, route_mode=auto, safety_mode=conservative, preferred_model=None, scene_hint=None, target_url=None)` and `TaskRead`.

`backend/secagent/repository.py`:

* `TaskRepository(session: Session)` exposes its `session` attribute.
* `create_task(payload: TaskCreate, owner_id: str | None = None, *, commit=True) -> TaskRead`; with `commit=False` it flushes only.
* `configure_task_budget(task_id, *, max_model_calls, max_input_tokens, max_output_tokens, max_steps) -> None`; all values must be nonnegative, and it flushes.
* `record_audit(actor_id, action, resource_type, resource_id, outcome, details=None, *, commit=True) -> AuditEventRow` delegates to `AuditService` and only commits when requested.

### Audit/event writer

`backend/secagent/services/audit.py`: `AuditService(session: Session)` and `record(actor_id, action, resource_type, resource_id, outcome, details=None, *, ip_address=None) -> AuditEventRow`.  It adds + flushes only; redacts details through `redact_audit_details` before JSON persistence.  This can compose in the service root transaction directly (or via `TaskRepository.record_audit(..., commit=False)`).

`backend/secagent/services/task_events.py`: `TaskEventService(session).append(task_id, event_type, payload, *, commit=True) -> int` is Task-specific and generic, with a 16 KiB redacted payload limit.  It is not the requested typed conversation event boundary; use only its redaction/size-policy precedent, not its row/table.

`backend/secagent/security/redaction.py`: `redact_mapping(value)`, `redact_audit_details(value)`, and `redact_text(value, *, include_generic_key=False)` are reusable recursive redaction primitives.  The audit variant is stricter (e.g. body/IP/exception keys).

### Legacy upload and ZIP security

`backend/secagent/services/storage.py`:

* `StorageService(data_dir: Path, *, upload_max_bytes: int, archive_max_files: int, archive_max_bytes: int)`.
* `workspace(task_id) -> Path` creates `data_dir/tasks/<task_id>`; `remove_workspace(task_id)` resolves/confines that task path then recursively deletes it.
* `async save_upload(task_id, upload: UploadFile) -> dict` streams 1 MiB chunks, enforces per-upload limit, writes random `<uuid><client suffix>` under `tasks/<task>/uploads`, hashes it, extracts `.zip` into `tasks/<task>/extracted`, and returns `{original_name, stored_name, sha256, size, extracted}`.  `UploadTooLarge` is the declared validation exception.

`backend/secagent/security/files.py`: `extract_zip_safely(archive: Path, destination: Path, *, max_files: int, max_bytes: int) -> list[Path]`; checks file count, aggregate expanded bytes, symlinks, absolute/traversal/drive paths, and resolved output confinement. `UnsafeArchive` marks rejection.

### Configuration and app/session wiring

`backend/secagent/config.py` provides `Settings.data_dir`, `upload_max_bytes`, `archive_max_files`, `archive_max_bytes`, legacy Task budget defaults (`max_model_calls_per_task`, `max_input_tokens_per_task`, `max_output_tokens_per_task`, `max_steps_per_task`), `max_replans`, and `task_timeout_seconds`; requested conversation limits are absent.

`backend/secagent/db.py`: `make_session_factory(database_url) -> sessionmaker[Session]`, with `expire_on_commit=False`. `make_engine` uses `check_same_thread=False` and enables `PRAGMA foreign_keys=ON` for SQLite.

`backend/secagent/main.py:create_app` stores `settings` and `session_factory` on `app.state`, but has no conversation router/service/storage entry. `backend/secagent/api/tasks.py:storage_for(request)` shows the present constructor wiring for legacy `StorageService`.

## Service construction and transaction constraints

The brief requires the service writer session to be the one and only session supplied to `ConversationRepository`, `TaskRepository`, typed conversation-event writer, and `AuditService`; test identity (`is`), not equivalence.  It additionally receives a separate `session_factory` used only by short read/preflight sessions.

At every public write entry the writer must have no active transaction and no `new`/`dirty`/`deleted` state.  Preflight must close before file staging.  Then the service opens the sole root transaction; repository mutation calls must use `commit=False`, Task creation/configuration must remain flush-only, and audit must be `AuditService.record` or `TaskRepository.record_audit(..., commit=False)`.  Do not call a repository method with default `commit=True` inside that root transaction.

For sending, the existing repository supports the critical concurrent part: `add_message(..., commit=False)` locks + checks idempotency, then `create_turn(..., commit=False)` locks + validates linked Task owner.  The service must create the legacy Task with the conversation owner, not the actor (an admin actor can otherwise fail the turn owner invariant).

## Existing concurrency fixtures/evidence

* `backend/tests/conftest.py:settings` creates file-backed SQLite (`tmp_path/test.db`) and the normal `app` fixture builds tables from `app.state.session_factory.kw["bind"]`.
* `backend/tests/unit/test_conversation_repository.py:repository_env` is the directly reusable file-backed SQLite fixture: engine from `make_engine(sqlite:///<tmp>/conversation-repository.db)`, `Base.metadata.create_all`, `sessionmaker(..., expire_on_commit=False)`, and seeded analyst/admin actors/users.
* That test already executes real threaded barriers with independent sessions: 12 parallel messages (contiguous sequences), 2 same-key message writers (one row/replay), 10 parallel turn creators (contiguous plan versions/active newest), and 8 concurrent events (visible cursor order). This is the correct local SQLite model for service race tests.
* No reusable real PostgreSQL transactional concurrency fixture exists. `test_conversation_schema.py` only emits offline PostgreSQL Alembic SQL; `test_sqlite_import.py`'s `empty_postgres_url` is an SQLite-looking target helper for import tests, not a live PostgreSQL service. The brief's required PostgreSQL READ COMMITTED barrier test needs environment/fixture work plus external recorded evidence.

## Brief-to-current-interface incompatibilities (report only)

1. No `ConversationService`, `ConversationStorageService`, typed conversation-event writer, conversation API router, or app-state constructor wiring currently exists.
2. The repository lacks the required owner-authorized, nonmutating lookup by `(conversation_id, idempotency_key)`; `get_message` needs a message ID, and `add_message` alone cannot perform the no-staging replay preflight.
3. `UserMessageCreate` accepts whitespace-only content and has no attachment-relative-path list; the prescribed submission/detail/reply DTOs do not exist.
4. `Settings` lacks all requested conversation limits and strict integer/bool validation. Existing `worker_concurrency` only is validated.
5. Legacy `StorageService` is not substitutable for the requested attachment store: it writes `tasks/<task>/...`, trusts the client suffix for its stored filename, returns `stored_name`/`size` rather than attachment `storage_ref`/`size_bytes`, has no attachment `content_type`, status or supplied relative path, and publishes before any conversation DB row. Its cleanup scope is task-only.
6. `extract_zip_safely` provides a sound baseline but does not enforce the brief's NFC normalization, per-segment length, ADS/trailing-dot-space, reserved-name strictness, case-fold collisions, or per-archive extraction isolation.
7. `ConversationRepository` does not reject messages for archived conversations; a service must impose and recheck that behavior after its write lock. It also has no message/attachment/turn aggregate-detail method, so detail composition requires existing individual reads/lists.
8. `ConversationEventCreate` is a generic arbitrary event/payload DTO and `append_event` persists it; the requested whitelist/16,384-byte typed event writer must sit above it. Current conversation events have no size/redaction cap.
9. The service compensation requirements cross DB and filesystem, while current APIs have no publish-intent/recovery marker. The brief explicitly leaves uncertain commit recovery for later; do not claim filesystem/DB global atomicity.

