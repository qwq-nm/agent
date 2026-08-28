# Conversation schema and validated DTOs

## Context

This is the first feature implementation checkpoint after baseline stabilization. It creates only the durable conversation schema and validated data contracts. Repository behavior, REST, upload I/O, SSE services, jobs, subtasks, and frontend changes are deferred.

## Public enum contracts

Add conversation-focused enums without changing legacy task enums:

- ConversationStatus: active, archived (default active)
- ConversationMessageRole: user, assistant, system
- ConversationMessageKind: user_text, assistant_status, plan, tool_approval, model_failure, assistant_answer, system_notice
- ConversationMessageStatus: pending, streaming, completed, failed, superseded (new user messages default completed)
- AttachmentStatus: pending, ready, failed
- ConversationTurnStatus: created, decomposing, scheduling, running, synthesizing, completed, waiting_tool_approval, waiting_model_decision, replan_requested, superseded, partial, failed_retryable, failed, cancelled

## Exact validation limits

- canonical UUID strings: parse as UUID and re-serialize to the lowercase 8-4-4-4-12 form; reject arbitrary 36-character text and unhyphenated values
- title: 1..160 trimmed characters; omitted create title means 新对话
- message content: 1..64,000 characters for create/write DTOs
- authorization scope: 0..4,000 characters
- allowed targets: at most 20 unique trimmed non-empty strings, each at most 2,048 characters
- requested parallelism: optional integer 1..3; it is only a request and later service code applies server limits
- idempotency key: 1..255 characters when present
- original filename: 1..255; content type: 1..255
- opaque storage reference: 1..500 and must be a normalized relative POSIX path with no absolute prefix, drive, NUL, empty/dot/dot-dot segment, or backslash
- optional client relative path: 1..1,000 under the same safe-relative-path rules
- attachment size: integer >= 0
- SHA-256: exactly 64 lowercase hexadecimal characters
- message sequence and turn plan version: integers >= 1
- event type: 1..80; optional subtask ID: 1..36. In this checkpoint it is an
  opaque future-DAG identifier, so canonical UUID validation does not apply to
  that field yet.

## Validated DTOs

Create a dedicated secagent.conversation_domain module with:

- ConversationSettings: authorization scope, existing SafetyMode, allowed targets, optional requested parallelism; conservative/empty defaults
- ConversationCreate has only caller-controlled title and settings; owner/status/active turn/counters are server-owned. ConversationPatch has optional title and optional whole-settings replacement, rejects explicit null for either field, rejects status/active-turn fields, and requires at least one supplied field. ConversationRead returns id, owner, title, status, structured settings, active turn ID, and timestamps without allocation counters.
- UserMessageCreate has only content. Internal ConversationMessageWrite has trusted role, kind, content, status, optional turn ID, and optional idempotency key; sequence/id/conversation are server-owned. ConversationMessageRead returns all persisted public message fields.
- TurnBudgetSnapshot with nonnegative server-snapshot fields for max subtasks, model calls/subtask, tool calls/subtask, timeout seconds, replans, and context tokens
- ConversationTurnCreate has trigger message ID, optional task ID, structured budget, and optional replan source; plan version/status are server-owned. ConversationTurnRead returns persisted turn fields with structured budget.
- AttachmentMetadataCreate has original name, storage reference, optional client relative path, content type, size, sha256, and status; message/id are server-owned. AttachmentRead adds those server fields.
- ConversationEventCreate has event type, structured payload, optional turn ID, and optional subtask ID; conversation/cursor/timestamp are server-owned. ConversationEventRead adds those server fields.

Read DTOs expose structured settings/budget/payload objects, never raw JSON strings. Event payload values must be strict JSON-compatible: object keys are strings and values reject non-serializable objects, NaN, and positive/negative Infinity. Provide one small shared canonical serializer/parser for validated JSON: UTF-8-safe json.dumps with ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False; parsing must validate back into the named DTO/type before use.

All request/write DTOs use `extra="forbid"`; silently ignoring server-owned or
unknown input is not allowed.

## SQLAlchemy schema

Add matching rows to backend/secagent/db_models.py:

1. conversations
   - String(36) UUID PK; non-null owner_id String(36) FK users.id RESTRICT + index
   - title String(160), status String(32) default/server-default active, settings_json Text default/server-default {}
   - nullable/indexed active_turn_id FK conversation_turns.id SET NULL
   - next_message_sequence and next_turn_plan_version Integers default/server-default 1 with named >=1 checks; later repositories use them as atomic allocation counters
   - non-null created/updated timezone timestamps with ORM and server defaults
2. conversation_messages
   - String(36) UUID PK; conversation_id String(36) FK CASCADE + index
   - sequence Integer with named >=1 check, role String(16), kind String(32), content Text, status String(32) default/server-default completed
   - nullable/indexed turn_id String(36) FK conversation_turns.id SET NULL
   - nullable idempotency key String(255)
   - non-null created/updated timezone timestamps with ORM and server defaults
   - unique (conversation_id, sequence) and (conversation_id, idempotency_key)
3. message_attachments
   - String(36) UUID PK; message_id String(36) FK CASCADE + index
   - original_name String(255), storage_ref String(500), optional relative_path String(1000), content_type String(255), size_bytes Integer with named >=0 check, sha256 String(64), status String(16) default/server-default ready
   - non-null created timezone timestamp with ORM and server default
4. conversation_turns
   - String(36) UUID PK; conversation_id String(36) FK CASCADE + index
   - trigger_message_id String(36) FK conversation_messages.id NO ACTION, DEFERRABLE INITIALLY DEFERRED + index
   - nullable task_id String(36) FK tasks.id SET NULL + index
   - plan_version Integer with named >=1 check, status String(32) default/server-default created, budget_json Text default/server-default {}
   - nullable/indexed self replan_from_turn_id String(36) FK SET NULL
   - non-null created/updated timestamps with ORM/server defaults and nullable started/finished timestamps
   - unique (conversation_id, plan_version)
5. conversation_events
   - globally increasing autoincrement BigInteger PK with an Integer SQLite variant, including after deletion of the highest prior event (sqlite_autoincrement=True)
   - conversation_id String(36) FK CASCADE + index
   - nullable/indexed turn_id String(36) FK SET NULL
   - nullable/indexed subtask_id String(36) reserved for the later DAG revision, with no FK yet
   - event_type String(80), payload_json Text default/server-default {}, and non-null created timestamp with ORM/server default

Except for fields explicitly described as nullable, every column is nullable=False. Give every FK and check an explicit stable name. Do not force use_alter on ORM foreign keys: SQLite Base.metadata.create_all must emit them inline, while SQLAlchemy can sort the PostgreSQL cycle. ORM defaults, server defaults, nullability, constraint names, deferrability, and ondelete behavior must match the migration. Cross-conversation relationship invariants will be enforced by the repository checkpoint; do not add unwieldy composite FKs in this task.

## Alembic revision

Add revision `20260826_09` with `down_revision = "20260826_08"`. Revision
`20260826_08` is the already-implemented provider-route timestamp correction and
must remain an immutable parent in the chain.

- Create conversations/messages initially with the circular FK columns but without those two constraints.
- Create attachments, turns, and events. The trigger-message FK is named, NO ACTION, DEFERRABLE INITIALLY DEFERRED.
- Add the named conversations.active_turn_id and conversation_messages.turn_id constraints using batch_alter_table, so real SQLite migration and PostgreSQL SQL generation both work.
- Downgrade first removes those two constraints, then drops events, attachments, turns, messages, and conversations in dependency-safe order.
- Preserve legacy tables and data.

Use the exact circular FK names
`fk_conversations_active_turn_id_conversation_turns` and
`fk_conversation_messages_turn_id_conversation_turns` in both ORM metadata and
the migration/offline-SQL assertions.

## TDD and verification

Write tests first and observe the correct RED before production code. Cover:

- exact enum values/defaults and every validation boundary above;
- safe path traversal/drive/backslash/NUL rejection and lowercase SHA validation;
- canonical structured JSON round trip plus rejection of non-string keys, non-JSON objects, NaN, and Infinity;
- all five tables, required columns, FK targets/ondelete, indexes, and unique contracts;
- event IDs remain increasing after deleting the row holding the highest ID;
- deleting a populated parent conversation cascades messages/turns despite their deferred trigger relationship, while directly deleting a still-referenced trigger message fails at transaction commit;
- Alembic upgrade from `20260826_08`, schema inspection, downgrade to
  `20260826_08`, re-upgrade, and `alembic check`;
- PostgreSQL offline alembic upgrade head --sql contains creation of the five tables and both named circular constraints.

Run focused tests, then the complete backend suite. Run git diff --check.

## Delivery

- Do not add a repository or API route.
- Do not alter legacy task behavior.
- Commit a focused change.
- Write RED/GREEN/test/self-review evidence to .superpowers/sdd/conversation-schema-report.md.
- Return only status, commit, one-line verification summary, and concerns.
