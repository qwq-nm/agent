# SecAgent-X Chinese User Manual Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one accurate Chinese manual that explains what SecAgent-X implements and guides administrators and analysts through setup, configuration, task execution, evidence review, and troubleshooting.

**Architecture:** Keep the manual as one task-oriented document at `docs/user-manual.md`, with role-specific notes embedded where needed. Add a discoverable link from `README.md`; do not change application behavior or duplicate the full deployment reference.

**Tech Stack:** Markdown, Windows PowerShell, Docker Compose, existing Vue 3 UI terminology, existing FastAPI operational endpoints.

## Global Constraints

- Use the current UI's exact Chinese menu, field, button, role, and task-status names.
- Document both `admin` and `analyst` workflows; clearly mark administrator-only operations.
- Treat OpenCode Go as the OpenAI-compatible backend for the internal DeepSeek route, never as a remote execution runtime.
- Never read, print, or add live API keys, database credentials, JWT values, encryption keys, or `.env` contents.
- Use placeholders only for provider credentials and state that `admin/admin` is local-demo-only.
- Keep deployment detail concise and link to `docs/deployment.md` for production operations.
- Do not modify backend, frontend, Compose, database, or runtime behavior.

---

### Task 1: Write the Combined Administrator and Analyst Manual

**Files:**
- Create: `docs/user-manual.md`
- Reference: `README.md`
- Reference: `docs/deployment.md`
- Reference: `docs/demo-script.md`
- Reference: `frontend/src/App.vue`
- Reference: `frontend/src/views/TaskCreateView.vue`
- Reference: `frontend/src/views/TaskDetailView.vue`
- Reference: `frontend/src/views/SystemView.vue`

**Interfaces:**
- Consumes: Current Docker Compose commands, environment variable names, UI labels, task states, model routing, tool policies, and report behavior.
- Produces: A standalone operator document linked by Task 2.

- [ ] **Step 1: Create the manual with a task-oriented table of contents**

Create `docs/user-manual.md` with these exact top-level sections:

```markdown
# SecAgent-X 使用与管理手册

## 1. 手册用途
## 2. 已完成的工作
## 3. Agent 如何工作
## 4. 五分钟启动
## 5. 登录、账号与角色
## 6. 配置 GLM 与 OpenCode Go
## 7. 创建并执行任务
## 8. 人工审批 Web 操作
## 9. 查看执行过程、证据与报告
## 10. 任务状态与操作
## 11. 安全边界与使用限制
## 12. 常见问题与排障
## 13. 常用运维命令
## 14. 验收结果与相关文档
```

The opening must identify the two audiences, state that authorization is required, and warn that security findings require human review.

- [ ] **Step 2: Document implementation and Agent data flow**

Describe the implemented FastAPI, Vue, PostgreSQL, Redis, and Celery stack; account/RBAC support; encrypted provider credential storage; three task scenes; approval flow; evidence ledger; reports; bounded replanning; and SSRF/redaction controls.

Use this exact execution sequence:

```text
用户目标与授权范围
  -> GLM 解析任务
  -> DeepSeek/OpenCode Go 制定受限计划
  -> 后端 RiskGate 校验并请求必要审批
  -> 白名单工具执行并写入 Evidence Ledger
  -> DeepSeek/OpenCode Go 复核证据，必要时有限重规划
  -> GLM 基于已保存证据生成中文报告
```

State that models cannot call Shell, bypass the tool registry, approve their own actions, or write unsupported facts into the final evidence basis.

- [ ] **Step 3: Document startup, login, accounts, and provider configuration**

Include the Docker quick start from the repository root:

```powershell
Copy-Item .env.example .env
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
docker compose up --build -d
docker compose ps
```

Document `http://127.0.0.1:18080`, local-demo login `admin/admin`, password reset through `Team`, and the `admin` versus `analyst` permission boundary.

For live models, document these non-secret OpenCode Go settings:

```env
MODEL_MODE=live
DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_STYLE=opencode-go
DEEPSEEK_REASONING_EFFORT=high
WEB_ALLOWED_HOSTS=web-demo
```

Then direct administrators to `模型与工具` -> `Provider 密钥`, enter the OpenCode Go key in the `deepseek` row and the Zhipu key in the `glm` row, select the save icon, and run `连通性检查`. Explain that only a key hint is returned after encrypted database storage.

- [ ] **Step 4: Document all three task workflows**

For each scene, provide a compact example table containing `任务目标`, `授权范围`, `场景`, `授权 URL`, and upload requirements where applicable.

Use these safe examples:

```text
日志响应：分析上传的 access.log，识别异常扫描并生成证据报告。
源码审计：只读审计上传的源码 ZIP，不安装依赖、不导入模块、不执行代码。
Web 分析：对 http://web-demo/ 执行授权的被动 HTTP 分析，不提交表单。
```

Explain `创建任务` -> fill form -> submit -> `开始执行`, and distinguish `auto` routing from manual model selection.

- [ ] **Step 5: Document approvals, observations, states, and security boundaries**

For Web approval, require the operator to verify `工具`, `风险`, and `参数/目标`, enter a reason, then choose `批准并继续` or `拒绝`. State that approval applies only to the current step and can be invalidated when step parameters change.

Include a status table for `created`, `queued`, `parsed`, `planned`, `running`, `waiting_human`, `paused`, `completed`, `failed_retryable`, `failed`, and `cancelled`, plus the actions exposed for each relevant state.

Explain the `证据`, `工具`, and `模型` tabs, report access, mock-result labeling, and the rule that evidence is the factual source of truth.

- [ ] **Step 6: Add troubleshooting and operational commands**

Cover provider not-ready, no task response, Web target rejected, approval waiting, retryable failure, upload rejection, and port conflict. Include these commands without printing secrets:

```powershell
docker compose ps
docker compose logs --tail 200 api
docker compose logs --tail 200 worker
docker compose logs --tail 200 frontend
Invoke-RestMethod http://127.0.0.1:18080/api/health/live
Invoke-RestMethod http://127.0.0.1:18080/api/health/ready
Invoke-RestMethod http://127.0.0.1:18080/api/models/status
docker compose restart api worker frontend
docker compose down
```

Warn against `docker compose down -v` unless permanent data deletion is intended.

- [ ] **Step 7: Verify required content and secrets**

Run:

```powershell
rg -n "^## (1|2|3|4|5|6|7|8|9|10|11|12|13|14)\." docs/user-manual.md
rg -n "admin/admin|OpenCode Go|Provider 密钥|waiting_human|Evidence Ledger|docker compose" docs/user-manual.md
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1
git diff --check
```

Expected: all 14 sections and required operational terms are present; `secret scan: clean`; `git diff --check` prints nothing.

- [ ] **Step 8: Commit the manual**

```powershell
git add docs/user-manual.md
git commit -m "docs: add SecAgent-X user manual"
```

### Task 2: Add Discovery Link and Run Final Documentation Checks

**Files:**
- Modify: `README.md`
- Test: `docs/user-manual.md`

**Interfaces:**
- Consumes: `docs/user-manual.md` from Task 1.
- Produces: A README entry point and a verified final documentation-only diff.

- [ ] **Step 1: Add the manual link to README**

Replace the final related-document sentence with:

```markdown
完整使用流程见 [docs/user-manual.md](docs/user-manual.md)，部署细节见 [docs/deployment.md](docs/deployment.md)，比赛演示顺序见 [docs/demo-script.md](docs/demo-script.md)。
```

- [ ] **Step 2: Verify referenced files and Markdown formatting**

Run:

```powershell
@('docs/user-manual.md', 'docs/deployment.md', 'docs/demo-script.md') | ForEach-Object { if (-not (Test-Path -LiteralPath $_)) { throw "Missing documentation target: $_" } }
rg -n "\[docs/(user-manual|deployment|demo-script)\.md\]\(docs/(user-manual|deployment|demo-script)\.md\)" README.md
git diff --check
```

Expected: all paths exist, README reports all three links on its final related-document line, and `git diff --check` prints nothing.

- [ ] **Step 3: Run the final secret scan and review scope**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_no_secrets.ps1
git status --short
git diff --stat HEAD~1
git diff -- README.md docs/user-manual.md
```

Expected: `secret scan: clean`; only the plan, design, manual, and README documentation changes are in the branch history or worktree; no runtime files changed.

- [ ] **Step 4: Commit the README entry point**

```powershell
git add README.md
git commit -m "docs: link user manual from README"
```

- [ ] **Step 5: Confirm the worktree is clean**

Run:

```powershell
git status --short --branch
```

Expected: branch header only, with no modified or untracked files.
