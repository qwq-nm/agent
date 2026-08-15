# Provider API Key Management Design

**Date:** 2026-08-15  
**Status:** Draft for user review  
**Scope:** Store and use DeepSeek and GLM API keys from the administrator console.

## Goal

Allow an administrator to enter, replace, clear, and connectivity-test the two
model-provider API keys from the existing "Model and Tools" page. A saved key
must be available to the API and Worker without a process restart. Plaintext
keys must never be returned to the browser, written to audit details, or
persisted in the database.

The existing local administrator bootstrap remains separate. The deployment
will create the requested `admin` administrator with password `admin` through
the existing CLI after the schema is ready. The web form is not responsible for
bootstrapping the first account.

## Chosen Approach

Store provider credentials in PostgreSQL encrypted with an application master
key held outside the database. Keep environment/Secret-file credentials as a
backward-compatible fallback. Build provider runtimes from a fresh credential
snapshot for each API provider operation and at the start of each Worker task.

This keeps the existing PostgreSQL ownership and audit boundaries, supports
multiple API and Worker processes, and makes rotation effective for new work
without coordinating process restarts.

The following alternatives are rejected:

- An encrypted shared file is harder to coordinate across replicas and database
  backups, and does not fit the existing persistence boundary.
- Plaintext database storage would expose keys through backups, SQL access, and
  accidental diagnostics.
- A full external secret-manager integration is outside the MVP deployment
  scope.

## Security Model

### Encryption key

Add `PROVIDER_CREDENTIAL_ENCRYPTION_KEY` and
`PROVIDER_CREDENTIAL_ENCRYPTION_KEY_FILE` to settings. The value is a Fernet
key and is loaded with the same file-first secret helper used by the existing
JWT and provider secrets. The application must fail closed for credential
read/write when this key is absent or invalid. The key is never stored in
PostgreSQL and is never exposed by an API response.

The local deployment creates the ignored Secret file during setup. Production
deployment supplies it as a Docker Secret or an equivalent protected
environment value. A known development default is not permitted for this
credential-encryption key.

### Stored data

Add a `provider_credentials` table with one row per supported provider:

| Column | Type | Purpose |
| --- | --- | --- |
| `provider` | string primary key | `deepseek` or `glm` |
| `encrypted_api_key` | text nullable | Fernet ciphertext only |
| `key_hint` | string nullable | Non-secret suffix for operator recognition |
| `state` | string | `configured` or explicit `cleared` override |
| `updated_by` | user foreign key nullable | Administrator who last changed it |
| `created_at` | timezone datetime | Creation time |
| `updated_at` | timezone datetime | Rotation/status time |

The row is retained as a tombstone when a credential is cleared: its encrypted
value and hint are set to null and its state becomes `cleared`. This preserves
the administrator's explicit decision and prevents an older environment
fallback from becoming active again. The key hint is derived from the
normalized key and contains at most the final four characters; short keys use
a generic masked value. No plaintext, ciphertext, or encryption key is
included in audit details or error messages.

### Access control and transport

All credential endpoints require the existing `require_admin` dependency.
Analysts receive the existing 403 contract. The browser sends the key only in
the HTTPS/local request body. The form uses a password input, clears its value
after a successful request, and does not retain the key in a Pinia store or
local/session storage.

## Backend API

Use the existing `/api/admin` router and fixed provider allowlist.

### List status

`GET /api/admin/provider-credentials`

Response:

```json
[
  {
    "provider": "deepseek",
    "configured": true,
    "key_hint": "...abcd",
    "updated_at": "2026-08-15T12:00:00Z"
  },
  {
    "provider": "glm",
    "configured": false,
    "key_hint": null,
    "updated_at": null
  }
]
```

The response never contains a write-only key, ciphertext, or encryption
metadata.

### Save or replace

`PUT /api/admin/provider-credentials/{provider}`

Request:

```json
{"api_key": "<write-only provider key>"}
```

Normalize surrounding whitespace, reject empty or multiline values, and apply
a bounded maximum length. Encrypt before the transaction is committed. Record
an audit event containing only the provider, operation, and outcome. Return the
same status representation as the list endpoint.

### Clear

`DELETE /api/admin/provider-credentials/{provider}`

Clear the encrypted value, hint, and active provider state while retaining the
explicit `cleared` row, revoke its use for all new runtime snapshots, record a
redacted audit event, and return no credential data.

### Connectivity check

Keep `POST /api/models/check` as the public console action, but make it resolve
the selected provider from the database credential store first and fall back to
the existing environment/Secret-file value only when no database row exists.
The response remains limited to provider, model, status, safe request ID,
token counts, latency, and stable error code.

## Runtime and Immediate Effect

Introduce a small credential-store/runtime-factory boundary rather than
putting database access into provider classes.

1. The API request opens a database session and reads/decrypts the selected
   credential when it needs a provider status or check.
2. The Worker opens its normal task session, reads/decrypts the current
   credentials before constructing the task's `ModelRouter`, and closes the
   provider HTTP client after task execution.
3. A task uses one credential snapshot for its own model calls. A later task
   observes a replacement or deletion immediately.
4. Environment/Secret-file values remain the fallback for deployments that
   have not migrated to the console. An absent database row uses the fallback;
   a `configured` row overrides it with the database value; a `cleared` row
   explicitly disables the fallback.

The application must be able to boot while the database credentials are empty
so the administrator can configure them. In `live` mode, missing providers are
reported as a safe authentication/configuration failure at request time and by
readiness; they must not silently become Mock. The liveness check used for
container startup remains available so the console can be reached while
readiness is degraded. Once both keys are saved, readiness becomes healthy and
new tasks use the real providers without a restart.

## Frontend Changes

Extend the existing admin-only `SystemView` provider sections with a credential
panel for each fixed provider:

- configured/not configured status;
- masked suffix and last-updated time when configured;
- write-only API key input;
- save/replace action;
- clear action with a confirmation step;
- existing connectivity-check action and safe result.

Loading the status must not cause a secret to be requested. Save and clear
operations refresh the status list and clear local input state. API errors use
the existing error-message pattern. No full key appears in visible text,
component state shared outside the form, or test snapshots.

## Configuration and Deployment

Add the encryption-key settings to `.env.example`, Docker Compose, production
Secret definitions, deployment documentation, and the secret scan. The
production API and Worker receive the same encryption Secret. The local setup
generates a random ignored file before the stack starts.

The existing provider base URLs and model names remain environment-configured
in this slice. The form manages only the two API keys; changing endpoints or
models remains a deployment configuration concern.

## Account Bootstrap

After migration, run the existing command in the API container:

```powershell
docker compose exec api python -m secagent.cli create-admin --username admin
```

Enter `admin` twice as requested. This is deliberately a local test credential
and must be changed before a shared or production deployment. The existing
web-admin password validation remains at eight characters for subsequently
created accounts.

## Error Handling

- Missing/invalid encryption master key: safe 503 configuration error for
  credential operations; never disclose the underlying key or parsing detail.
- Invalid provider name: 422/404 using the existing API error conventions.
- Empty, multiline, or oversized API key: 422 validation error.
- Invalid ciphertext: treat the provider as unavailable and record only a
  stable configuration error.
- Provider authentication, timeout, rate-limit, and schema failures continue
  through the existing sanitized ProviderFailure mapping.
- Database write failure rolls back the credential change and audit event.

## Testing and Acceptance

Backend tests must cover:

- Fernet encrypt/decrypt round trip and invalid master-key behavior;
- database rows and API responses contain no plaintext key;
- admin save/list/replace/clear and analyst denial;
- key validation and provider allowlist;
- audit redaction and safe error contracts;
- provider check precedence (database over environment fallback);
- Worker/API runtime loading after a rotation without process restart;
- migration upgrade/downgrade and existing schema tests.

Frontend tests must cover:

- masked status rendering;
- save/replace/clear request bodies and input clearing;
- no secret rendering after save;
- connectivity result and error states.

Acceptance requires:

1. `admin/admin` can log in and reach the credential panel.
2. An analyst cannot list, save, clear, or test credentials.
3. Both provider keys can be saved and replaced from the page.
4. A new task or provider check uses a newly saved key without restarting API
   or Worker.
5. Clearing a key makes the provider unavailable for new work and does not
   expose the cleared value.
6. Existing offline tests, frontend tests, build, migration checks, and secret
   scans remain green.

## Out of Scope

- Storing arbitrary third-party providers;
- storing provider base URLs or model names in the database;
- revealing a key after save;
- per-user provider keys or billing/quota management;
- external secret-manager integrations;
- automatic key rotation with a provider vendor.
