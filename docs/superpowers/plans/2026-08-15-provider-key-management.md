# Provider API Key Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an administrator-only web form and API that encrypts DeepSeek and GLM API keys in PostgreSQL, makes rotations effective for new API/Worker work without restart, and preserves the requested admin/admin bootstrap.

**Architecture:** Add a Fernet-backed credential store with a master key supplied only by deployment configuration. A ProviderRuntimeFactory resolves database credentials, explicit clear tombstones, and environment fallbacks before building a ModelRouter; API checks/status and each Worker task use a fresh router snapshot. Extend the existing SystemView and admin router without adding a second authentication system.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy/Alembic, PostgreSQL/SQLite test fixtures, cryptography.fernet.Fernet, Vue 3, TypeScript, Vitest, pytest.

## Global Constraints

- Provider scope is fixed to deepseek and glm; arbitrary providers are out of scope.
- Plaintext API keys must never be returned to the browser, persisted in PostgreSQL, or written to audit details/logs.
- PROVIDER_CREDENTIAL_ENCRYPTION_KEY or its file variant is deployment-only; no known development default is allowed.
- Database credentials take precedence over environment fallbacks; a cleared database tombstone disables the fallback.
- live mode must never silently fall back to Mock; missing providers fail safely at request/readiness time while liveness remains reachable.
- A saved or cleared credential must affect new API checks and Worker tasks without restarting API or Worker processes.
- All credential endpoints require the existing require_admin dependency; analysts receive 403.
- Keep existing provider base URLs/models environment-configured and keep admin/admin bootstrap separate from the web form.
- Use test-first cycles: write one focused failing test, run it and observe the expected failure, implement the smallest change, rerun the focused test, then run the relevant regression suite.

---

## File Map

Create:

- backend/secagent/security/provider_credentials.py: provider allowlist, key normalization, Fernet cipher, safe key hint.
- backend/secagent/services/provider_credentials.py: SQLAlchemy credential store, fallback resolution, save/clear status objects.
- backend/secagent/providers/runtime.py: database-aware ProviderRuntimeFactory.
- migrations/versions/20260815_05_provider_credentials.py: provider_credentials migration.
- backend/tests/unit/test_provider_credentials.py: cipher and store unit contracts.
- backend/tests/integration/test_provider_credentials_api.py: admin API, RBAC, audit, and no-leak contracts.
- backend/tests/integration/test_dynamic_provider_runtime.py: API/Worker refresh behavior.
- frontend/tests/provider-credentials.spec.ts: form behavior and secret non-rendering.

Modify:

- pyproject.toml, backend/secagent/config.py, backend/secagent/db_models.py.
- backend/secagent/providers/__init__.py, backend/secagent/providers/router.py, backend/secagent/main.py, backend/secagent/api/system.py, backend/secagent/api/admin.py, backend/secagent/worker.py.
- backend/tests/conftest.py, backend/tests/unit/test_config_secrets.py, backend/tests/unit/test_model_router.py, backend/tests/unit/test_schema.py, backend/tests/integration/test_readiness.py.
- frontend/src/types.ts, frontend/src/api/client.ts, frontend/src/views/SystemView.vue, frontend/src/styles.css.
- .env.example, docker-compose.yml, docker-compose.prod.yml, docs/deployment.md, README.md, scripts/check_no_secrets.ps1, and the ignored Secret example.

## Task 1: Add Credential Cryptography and Settings

**Files:**

- Create backend/secagent/security/provider_credentials.py
- Create backend/tests/unit/test_provider_credentials.py
- Modify pyproject.toml, backend/secagent/config.py, backend/tests/unit/test_config_secrets.py, .env.example

**Interfaces:**

- SUPPORTED_PROVIDERS: tuple[str, str] = ("deepseek", "glm")
- normalize_api_key(value: str) -> str
- key_hint(value: str) -> str
- ProviderCredentialCipher.from_settings(settings: Settings) -> ProviderCredentialCipher
- ProviderCredentialCipher.encrypt(value: str) -> str
- ProviderCredentialCipher.decrypt(value: str) -> str
- Settings.provider_credential_key() -> str | None

- [ ] Step 1: Write the failing crypto/settings tests.

Add tests with a generated Fernet key and these assertions:

~~~python
def test_cipher_round_trip_never_stores_plaintext() -> None:
    settings = Settings(
        provider_credential_encryption_key=Fernet.generate_key().decode()
    )
    cipher = ProviderCredentialCipher.from_settings(settings)

    encrypted = cipher.encrypt("provider-secret-123")

    assert encrypted != "provider-secret-123"
    assert cipher.decrypt(encrypted) == "provider-secret-123"


def test_normalize_rejects_multiline_and_blank_keys() -> None:
    with pytest.raises(ValueError):
        normalize_api_key("  \n")
    with pytest.raises(ValueError):
        normalize_api_key("key\nwith-newline")


def test_missing_or_invalid_master_key_is_configuration_error() -> None:
    with pytest.raises(CredentialEncryptionConfigurationError):
        ProviderCredentialCipher.from_settings(Settings())
~~~

Also add file-first and blank-value assertions for the new settings accessor in test_config_secrets.py.

- [ ] Step 2: Run the focused tests and verify the expected missing-symbol failure.

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_config_secrets.py -v
~~~

Expected: the new test module fails because the cipher and settings accessor do not exist yet; existing config tests remain importable.

- [ ] Step 3: Write the smallest crypto boundary.

Add cryptography>=43,<47 as a direct dependency. Implement ProviderCredentialCipher with Fernet(key.encode("ascii")), convert invalid key/token errors to CredentialEncryptionConfigurationError, strip only surrounding whitespace before validation, reject empty/multiline values, cap values at 512 characters, and return "***" for keys of four or fewer characters or "..." + value[-4:] otherwise. Add the two optional settings fields and accessor using read_secret.

- [ ] Step 4: Run focused tests and dependency import checks.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_config_secrets.py -v
~~~

Expected: all focused tests pass and no plaintext value appears in exception text.

- [ ] Step 5: Commit the crypto boundary.

~~~powershell
git add pyproject.toml backend/secagent/security/provider_credentials.py backend/secagent/config.py backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_config_secrets.py .env.example
git commit -m "feat: add provider credential encryption"
~~~

## Task 2: Persist Encrypted Credential Tombstones

**Files:**

- Modify backend/secagent/db_models.py
- Create migrations/versions/20260815_05_provider_credentials.py
- Create backend/secagent/services/provider_credentials.py
- Create/modify backend/tests/unit/test_provider_credentials.py and backend/tests/unit/test_schema.py

**Interfaces:**

- ProviderCredentialStatus(provider: str, configured: bool, key_hint: str | None, updated_at: datetime | None)
- ProviderCredentialStore(session: Session, settings: Settings)
- ProviderCredentialStore.list_status() -> list[ProviderCredentialStatus]
- ProviderCredentialStore.resolve_keys(fallbacks: Mapping[str, str | None]) -> dict[str, str | None]
- ProviderCredentialStore.save(provider: str, api_key: str, actor_id: str) -> ProviderCredentialStatus
- ProviderCredentialStore.clear(provider: str, actor_id: str) -> ProviderCredentialStatus

- [ ] Step 1: Write failing store and schema tests.

Use a SQLite test database. Cover saving a configured row whose encrypted_api_key differs from the input; list_status returning only status/hint/time; resolve_keys choosing the database value over a fallback; clear leaving a cleared tombstone and resolving the provider to None; an absent row using the fallback; and unsupported providers/bad keys raising ValueError.

Inspect the row directly and verify the raw key is not present in its serialized values. Extend REQUIRED_COLUMNS and required-table assertions in test_schema.py for provider_credentials, its state, and its updated_by foreign key.

- [ ] Step 2: Run the tests and verify the missing table/service failure.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_schema.py -v
~~~

Expected: store tests fail because the ORM table/service does not exist; schema assertions identify the new missing contract.

- [ ] Step 3: Add the ORM row and Alembic migration.

Add ProviderCredentialRow with provider as primary key, nullable encrypted_api_key, nullable key_hint, state configured/cleared, nullable updated_by referencing users.id with RESTRICT, and timezone-aware created_at/updated_at. Create migration 20260815_05_provider_credentials.py with down_revision = "20260815_04", matching foreign keys, and a downgrade that drops only this table.

- [ ] Step 4: Implement the store transaction boundary.

Use the cipher from Task 1. save normalizes and encrypts before flush, sets state="configured", derives the hint, sets actor/time, and commits one audit event containing only provider and operation. clear sets ciphertext/hint to None, state to "cleared", updates actor/time, and commits an audit event. resolve_keys treats absent rows as environment fallback, configured rows as decrypted database values, and cleared rows as explicit None. Roll back on database or cipher errors and expose only a stable configuration exception.

- [ ] Step 5: Run unit, schema, and migration checks.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_schema.py -v
.\.venv\Scripts\python.exe -m alembic upgrade head --sql
~~~

Expected: store/schema tests pass; generated SQL contains the new table and downgrade remains reversible.

- [ ] Step 6: Commit persistence.

~~~powershell
git add backend/secagent/db_models.py backend/secagent/services/provider_credentials.py migrations/versions/20260815_05_provider_credentials.py backend/tests/unit/test_provider_credentials.py backend/tests/unit/test_schema.py
git commit -m "feat: persist encrypted provider credentials"
~~~

## Task 3: Load Credentials Dynamically in API and Worker Runtimes

**Files:**

- Create backend/secagent/providers/runtime.py
- Modify backend/secagent/providers/__init__.py, backend/secagent/providers/router.py, backend/secagent/main.py, backend/secagent/api/system.py, backend/secagent/worker.py, backend/secagent/api/tasks.py
- Create backend/tests/integration/test_dynamic_provider_runtime.py
- Modify backend/tests/unit/test_model_router.py, backend/tests/integration/test_readiness.py, backend/tests/conftest.py

**Interfaces:**

- ProviderRuntimeFactory(settings: Settings).build(session: Session) -> ModelRouter
- build_providers(settings, *, client=None, provider_keys: Mapping[str, str | None] | None = None, allow_missing_live: bool = False) -> dict[str, ModelProvider]
- ModelRouter(..., allow_missing: bool = False)
- execute_queued_task(..., router: ModelRouter | None, settings: Settings | None = None)

- [ ] Step 1: Write failing runtime-refresh tests.

Save a database DeepSeek key, build a runtime, and assert the provider adapter carries that key; replace the row, build again without recreating the app, and assert the new key is used. Add a live-mode boot test with no database rows that reaches /api/health/live, reports readiness failure, and does not instantiate Mock for a live model call. Add a Worker fixture that builds its router at task start and closes the HTTP client after execution.

- [ ] Step 2: Run the focused runtime tests to verify the current static-router failure.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_dynamic_provider_runtime.py backend/tests/unit/test_model_router.py backend/tests/integration/test_readiness.py -v
~~~

Expected: the new factory/imports are missing and the existing worker runtime still reuses its startup router, demonstrating the behavior to change.

- [ ] Step 3: Add the runtime factory and deferred live validation.

Implement ProviderRuntimeFactory.build by resolving database/fallback keys in the supplied session and calling build_providers with provider_keys and allow_missing_live=True. Add allow_missing_live to build_providers and allow_missing to ModelRouter; retain strict defaults for direct callers and existing provider-contract tests. A deferred live router must still raise ProviderUnavailable(provider, AUTH) when a missing fixed provider is requested.

- [ ] Step 4: Replace startup-only provider use at request/task boundaries.

create_app stores the factory and builds only a deferred initial router so an empty credential table does not prevent liveness. POST /api/models/check and GET /api/models/status build a fresh router from a request session and always await router.aclose() in finally. Readiness resolves database credentials before checking both fixed live providers and keeps /api/health/live independent. The Worker builds a router inside execute_queued_task from the same task session, uses it for the whole task snapshot, closes it in finally, and stops caching a provider client in _WorkerRuntime; retain the runner/registry lock behavior. Keep the enqueue-side app.state.model_router only as the existing test/mock dependency because enqueue does not call a model.

- [ ] Step 5: Run focused runtime and regression tests.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_dynamic_provider_runtime.py backend/tests/unit/test_model_router.py backend/tests/integration/test_readiness.py backend/tests/integration/test_fixed_model_stages.py -v
~~~

Expected: all pass, including existing strict live-router and fixed-stage contracts; no client remains open after a request/task.

- [ ] Step 6: Commit dynamic runtime loading.

~~~powershell
git add backend/secagent/providers backend/secagent/main.py backend/secagent/api/system.py backend/secagent/api/tasks.py backend/secagent/worker.py backend/tests/conftest.py backend/tests/unit/test_model_router.py backend/tests/integration/test_readiness.py backend/tests/integration/test_dynamic_provider_runtime.py
git commit -m "feat: load provider credentials without restart"
~~~

## Task 4: Expose Admin Credential API

**Files:**

- Modify backend/secagent/api/admin.py and backend/secagent/api/errors.py
- Create backend/tests/integration/test_provider_credentials_api.py

**Interfaces:**

- GET /api/admin/provider-credentials -> list[ProviderCredentialRead]
- PUT /api/admin/provider-credentials/{provider} with {"api_key": string}
- DELETE /api/admin/provider-credentials/{provider}
- ProviderCredentialRead(provider, configured, key_hint, updated_at)

- [ ] Step 1: Write failing endpoint tests.

Use admin_client and analyst_client. Assert that admin can save, replace, list, and clear without a raw key in the response; the list shows the masked suffix and configured=false after clear. Assert that an analyst receives 403 for list and save. Also assert multiline/empty/provider-name validation, provider.credentials.save/provider.credentials.clear audit actions, and no test key in audit JSON.

- [ ] Step 2: Run endpoint tests and verify missing-route failures.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_provider_credentials_api.py -v
~~~

Expected: endpoint tests fail because the routes and response models are not present.

- [ ] Step 3: Implement admin-only response models and routes.

Add a literal provider path allowlist, ProviderCredentialWrite with a 1-512 character body field, and the list/save/clear validation paths. Convert missing/invalid encryption configuration to ApiError(503, "credential_storage_unavailable", "Provider credential storage is unavailable"); never pass underlying exception text to the client. Use the authenticated admin ID for store writes and return only ProviderCredentialRead.

- [ ] Step 4: Run API and audit regression tests.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_provider_credentials_api.py backend/tests/integration/test_audit_events.py backend/tests/integration/test_admin_users.py -v
~~~

Expected: all pass with no raw key in response bodies, stored audit JSON, or error messages.

- [ ] Step 5: Commit the admin API.

~~~powershell
git add backend/secagent/api/admin.py backend/secagent/api/errors.py backend/tests/integration/test_provider_credentials_api.py
git commit -m "feat: add admin provider credential API"
~~~

## Task 5: Add the Administrator Web Form

**Files:**

- Modify frontend/src/types.ts, frontend/src/api/client.ts, frontend/src/views/SystemView.vue, frontend/src/styles.css
- Create frontend/tests/provider-credentials.spec.ts

**Interfaces:**

- ProviderName = 'deepseek' | 'glm'
- ProviderCredential { provider: ProviderName; configured: boolean; key_hint?: string | null; updated_at?: string | null }
- api.listProviderCredentials(): Promise<ProviderCredential[]>
- api.saveProviderCredential(provider: ProviderName, apiKey: string): Promise<ProviderCredential>
- api.clearProviderCredential(provider: ProviderName): Promise<void>

- [ ] Step 1: Write failing Vue tests.

Mock the three new API methods and mount SystemView. Assert that a configured row renders only ...abcd, a save submits the typed value then clears the password input, clear asks for confirmation and calls the delete method, and the wrapper never renders a complete test key. Assert that the existing provider-check button still calls api.providerCheck and displays safe metadata.

- [ ] Step 2: Run the focused frontend test and verify missing API/UI behavior.

~~~powershell
cd frontend
npm run test -- --run tests/provider-credentials.spec.ts
cd ..
~~~

Expected: the new test fails because the API methods and credential controls do not exist.

- [ ] Step 3: Add types and same-origin API methods.

Extend types.ts with ProviderName and ProviderCredential. Add JSON GET, PUT, and DELETE methods in api/client.ts; send the API key only in the PUT body and use the existing apiRequest authentication/refresh path. Do not add persistence to the auth store or browser storage.

- [ ] Step 4: Add the form to SystemView with explicit local secret clearing.

Load credential statuses alongside the existing Promise.allSettled calls. Render one row per fixed provider with a type=password input named provider-key-{provider}, save/replace and clear buttons, status/hint/time, and a confirmation dialog for clear. Keep typed values in a local ref only; set the value to an empty string after success or failure completion. Disable only the active provider's controls while its request is running. Preserve existing check results and error patterns.

- [ ] Step 5: Add restrained responsive styling and run frontend tests/build.

Add credential-grid, credential-row, and credential-actions styles plus mobile rules without introducing a nested card or unstable widths.

~~~powershell
cd frontend
npm run test -- --run tests/provider-credentials.spec.ts tests/admin-views.spec.ts
npm run build
cd ..
~~~

Expected: focused tests and TypeScript/Vite build pass; the rendered DOM contains no full key.

- [ ] Step 6: Commit the frontend slice.

~~~powershell
git add frontend/src/types.ts frontend/src/api/client.ts frontend/src/views/SystemView.vue frontend/src/styles.css frontend/tests/provider-credentials.spec.ts
git commit -m "feat: add provider key management form"
~~~

## Task 6: Wire Deployment, Secret Generation, and Documentation

**Files:**

- Create secrets/provider_credential_encryption_key.txt.example
- Modify .env.example, docker-compose.yml, docker-compose.prod.yml, docs/deployment.md, README.md, scripts/check_no_secrets.ps1, scripts/offline_acceptance.ps1

- [ ] Step 1: Write configuration/compose contract checks.

Extend test_config_secrets.py to assert the new setting names and compose text. Add a test that production Compose defines the same encryption Secret for API and Worker and that the secret scan assignment regex recognizes PROVIDER_CREDENTIAL_ENCRYPTION_KEY without scanning the example file.

- [ ] Step 2: Run checks before configuration edits and observe missing-entry failures.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_config_secrets.py -v
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1
~~~

Expected: the new assertions fail because Compose/docs/secret definitions are not present; the existing scan remains clean.

- [ ] Step 3: Add deployment wiring without a known key default.

Add blank environment fields to .env.example. Pass the encryption key setting to API and Worker in base Compose when supplied. Add provider_credential_encryption_key to production Docker Secret definitions and mount the same read-only file into both services. Keep real provider keys and the encryption master key out of tracked files. The example file must contain instructions rather than a usable Fernet value.

- [ ] Step 4: Document local generation, live operation, and bootstrap.

Document this exact local generation flow:

~~~powershell
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
~~~

Document that the generated file is ignored, production uses a Docker Secret, MODEL_MODE=live reports not-ready until both database keys exist, and the existing command creates admin/admin after migration. State that the browser form is the place to enter provider keys and HTTPS is required outside local testing.

- [ ] Step 5: Run Compose parsing and secret scan.

~~~powershell
docker compose config
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1
~~~

Expected: both configurations parse, the scan prints secret scan: clean, and no key value is printed.

- [ ] Step 6: Commit deployment documentation.

~~~powershell
git add secrets/provider_credential_encryption_key.txt.example .env.example docker-compose.yml docker-compose.prod.yml docs/deployment.md README.md scripts/check_no_secrets.ps1 scripts/offline_acceptance.ps1 backend/tests/unit/test_config_secrets.py
git commit -m "docs: configure provider credential storage"
~~~

## Task 7: End-to-End Verification and Requested Account Bootstrap

**Files:**

- No new source files; use the committed implementation and local ignored Secret files.

- [ ] Step 1: Run the complete offline regression suite.

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/offline_acceptance.ps1
~~~

Expected: backend coverage remains at least 85%, frontend tests/build pass, and the final line is offline acceptance: passed.

- [ ] Step 2: Apply the migration and create the requested administrator.

Start the development stack after generating the encryption master key, then run:

~~~powershell
docker compose up --build -d
docker compose exec api python -m secagent.cli create-admin --username admin
~~~

Enter admin at both prompts. If the username already exists, verify its role and use the existing account rather than creating a duplicate.

- [ ] Step 3: Verify the protected API workflow without real keys in command output.

Log in as admin, call the credential list endpoint, save a locally supplied test key through the browser/API, confirm the response contains only status/hint, run provider check, replace the key, clear it, and confirm the provider is unavailable for new work. Use a temporary local test value or the user’s own browser entry; never echo a real key in PowerShell output.

- [ ] Step 4: Run final targeted security checks.

~~~powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/test_provider_credentials_api.py backend/tests/integration/test_dynamic_provider_runtime.py backend/tests/integration/test_audit_events.py -v
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1
git diff --check HEAD~7..HEAD
git status --short --branch
~~~

Expected: all targeted tests pass, the secret scan is clean, diff check has no output, and only ignored local Secret files remain outside Git.

- [ ] Step 5: Commit only a verified follow-up fix.

If a verification failure requires a source change, add a regression test first, rerun the failing and full relevant suite, then commit it with a message naming the defect. Do not claim completion until the fresh verification commands above exit with code 0.

## Handoff

After this plan is reviewed, execute one task at a time with either superpowers:subagent-driven-development or superpowers:executing-plans. Each task ends at its own green test cycle and commit; the final handoff must include the local URL, account bootstrap result, and exact verification commands that passed.
