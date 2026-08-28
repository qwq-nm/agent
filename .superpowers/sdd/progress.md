# Conversational multi-agent implementation progress

- Workspace hygiene: complete (`3a2d9ff3`; generated dependencies, caches, scratch reports, and test databases removed).
- Approved design imported: complete (`41b9deb0..1e7fcccc`).
- Clean baseline verification: complete (`28da8fea..168c7656`; frontend 41/41 + build, backend 304/304, independent reviews clean; one documented third-party warning).
- Frontend baseline stabilization: complete (`1e7fcccc..960d68e9`, 41/41 tests and build passed, independent review clean).
- Conversation schema and validated DTOs: complete (`bad6dc11..2560a615`; 17 focused / 321 backend tests, three independent reviews clean).
- Conversation repository: fix round 1/5 complete (4 addressed, 0 open;
  commits `81a96b32..53aecc94`; focused 24/24, backend 345/345; scoped
  spec/quality and concurrency re-reviews clean).
- Conversation repository: complete (`2560a615..53aecc94`, review clean).
- Ruling: event allocation is serialized per conversation, not globally —
  `events_after` is conversation-scoped, so cross-conversation ID gaps are safe;
  if a future global consumer is added it will need a separate global cursor
  protocol.
- Ruling: conversation service writers own one clean request-scoped root
  transaction, while authorization/idempotency preflight uses a separate short
  read session closed before staging — this keeps uploads outside database
  transactions; callers cannot wrap these writers in an outer transaction.
- Ruling: a compatibility Task is always owned by the conversation owner — an
  admin may act on the conversation but is represented only as the audit actor,
  preserving the repository's owner invariant.
- Ruling: DB/filesystem compensation cleans files only before commit starts or
  after a database-confirmed non-commit; an uncertain COMMIT result preserves
  files, invalidates the Session, and requires later reconciliation — deleting
  then could corrupt already-committed attachment metadata, so this checkpoint
  must not claim global cross-resource atomicity.
- Ruling: conversation events expose typed payload writers only; arbitrary
  caller payload persistence is outside the service API, so privacy and the
  16-KiB boundary are enforced before repository mutation.
- Conversation API/service: service and attachment-storage preflight revision
  passed after two scoped review rounds; implementation is committed at
  `69cdae2e` with report commit `5eebdccd`; independent task review found
  Critical 1 / Important 1 / Minor 1. Fix round 1/5 is committed at
  `2fc0fa0c`; targeted 29/29, non-PG full backend 413 passed + 1 expected
  skip, live PostgreSQL barrier 1/1, and live-PG focused 105/105 passed.
- Conversation service and attachment storage: fix round 1/5 complete
  (3 addressed, 0 open; commit `2fc0fa0c`; scoped re-review clean).
- Conversation service and attachment storage: complete
  (`53aecc94..2fc0fa0c`, review clean).
- Conversation HTTP/multipart/SSE: implementation brief written at base
  `2fc0fa0c`; initial preflight found Critical 2 / Important 4 contract gaps
  around query-ticket log redaction, multipart cleanup ownership, ticket
  infrastructure errors, bearer/burn ordering, and manual validation. All six
  are incorporated into the brief; scoped preflight re-review passed with all
  six addressed and no new Critical/Important findings. Implementation is in
  progress. The first implementer was interrupted by a Codex usage limit after
  ticket/log RED->18 GREEN and REST/message 17-route-404 RED; its uncommitted
  work remains intact. A replacement implementer is continuing from the
  recorded recovery handoff, with SSE state treated as unverified. The
  checkpoint is now committed at `980270ad`; focused 111/111 and full backend
  512 passed + 1 skipped. Independent task review found Critical 0 /
  Important 4 / Minor 0: inactive+matching-bearer did not burn the ticket,
  real signing-config failure used the generic 503 code, oversized cursors
  could escape as 500/stream failure, and the mandatory boundary test matrix
  was incomplete. Fix round 1/5 committed at `966fdec7`; focused 121/121,
  full backend 522 passed + 1 skipped, and scoped re-review found all four
  addressed with no new Critical/Important/Minor findings.
- Conversation HTTP/multipart/SSE: complete
  (`2fc0fa0c..966fdec7`, review clean).
- Ruling: a real PostgreSQL READ COMMITTED barrier result is a hard acceptance
  gate for concurrent replay. Windows has no reachable daemon, PostgreSQL
  service, or `psql`, but WSL Ubuntu can reach Docker Desktop server 29.5.2;
  use a task-scoped ephemeral PostgreSQL container and remove it after the run.
- Decomposition and capability routing: pending.
- Durable DAG execution and recovery: pending.
- Chat workspace frontend: pending.
- End-to-end verification and review: pending.
