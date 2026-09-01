# SecAgent-X

SecAgent-X 是一个具备自主决策能力、但受明确安全边界约束的网络安全智能体 MVP。它支持三条可演示的闭环：日志事件响应、静态源码审计、被动 Web 分析。模型仅负责理解、规划、复核和报告；白名单工具负责产生可追溯事实，所有关键结论都会写入 Evidence Ledger。

## 已实现能力

- GLM：中文任务解析与中文报告；DeepSeek：技术规划与证据复核。
- `auto`、`live`、`mock` 三种模型模式；Mock 结果始终标为“演示结果”。
- 日志链：格式识别、结构化解析、`WEB-SCAN-002` 攻击模式、时间线。
- 源码链：只读静态扫描、危险执行函数、硬编码密钥、危险配置；绝不导入或执行上传代码。
- Web Agent：支持受控 GET/POST/HEAD/OPTIONS、逐跳重定向复检、请求/响应限长与脱敏；中风险请求必须人工审批，证据不足时最多按 `MAX_REPLANS` 补充规划。
- PostgreSQL 任务状态、工具/模型调用、审批、证据和 Markdown 报告；Redis/Celery 负责排队和 Worker 执行。
- Vue 3 控制台：任务创建、决策时间线、证据账本、人工审批、报告和系统状态。

## Windows 本地开发

要求 Python 3.12+、Node.js 24+。

```powershell
cd F:\codex\secagent-x
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
$env:MODEL_MODE="mock"
.\.venv\Scripts\python.exe -m uvicorn secagent.main:app --reload --port 8000
```

另开 PowerShell：

```powershell
cd F:\codex\secagent-x\frontend
npm ci
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。本地 Web 场景默认只允许 Docker 内的 `web-demo`；若要测试公网 URL，请在 `.env` 中显式配置授权域名，且 URL 守卫仍会拒绝私网、回环、链路本地和元数据地址。

## Docker 一键运行（CPU）

```powershell
cd F:\codex\secagent-x
Copy-Item .env.example .env
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
python scripts\build_demo_archives.py
docker compose up --build -d
docker compose ps
```

打开 `http://127.0.0.1:18080`。如端口冲突，可在 `.env` 中修改 `SECAGENT_PORT`。停止服务：

```powershell
docker compose down
```

数据保存在命名卷 `secagent-data` 中；不要使用 `docker compose down -v`，除非明确要删除任务、证据和报告。

## 模型模式

| 模式 | 行为 |
|---|---|
| `mock` | 完全离线、确定性演示，不调用外部模型；界面和报告显示“演示结果”。 |
| `auto` | 按阶段优先 GLM/DeepSeek；已配置模型不可用时尝试另一模型，最后降级到显式 Mock。 |
| `live` | 只使用已配置的真实 GLM/DeepSeek；失败时不降级到 Mock。 |

真实模式下，管理员应在浏览器控制台的 Provider 凭据表单中录入 GLM 和 DeepSeek key；它们会加密保存到数据库中。未提交的 `.env` 或文件 Secret 中的 `GLM_API_KEY`、`DEEPSEEK_API_KEY` 仍可作为向后兼容的回退。`MODEL_MODE=live` 在两个数据库凭据保存前会保持 readiness not-ready，但 liveness 仍可让控制台、Worker 和前端启动。`secrets\provider_credential_encryption_key.txt` 是 Git 忽略的本地生成文件，Docker 启动前必须存在。除本地测试外，请通过 HTTPS 访问控制台。项目不使用 GPT。

OpenCode Go 作为 DeepSeek 路由的 OpenAI-compatible 后端时，可在 `.env` 中配置：

```env
MODEL_MODE=live
DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_STYLE=opencode-go
DEEPSEEK_REASONING_EFFORT=high
# 可选：把频繁的短阶段（拆解/子任务）的推理强度调低，显著降低每条消息的
# 拆解延迟（实测 high 时单次拆解约 100~140 秒）；最终综合仍用上面的值。
DEEPSEEK_DECOMPOSE_REASONING_EFFORT=low
DEEPSEEK_SUBTASK_REASONING_EFFORT=low
WEB_ALLOWED_HOSTS=web-demo,node4.anna.nssctf.cn
```

API key 仍通过管理员网页表单录入并加密保存；不要把真实 key 写进 `.env.example`、文档、日志或提交记录。`WEB_ALLOWED_HOSTS` 必须使用逗号分隔的精确授权主机名，端口写在任务 URL 中，不写入该列表。

迁移完成后可运行 `docker compose exec api python -m secagent.cli create-admin --username admin`，并在两次提示中输入 `admin` 来创建本地 `admin/admin`。这是演示初始账号，首次登录后必须更改密码，绝不能用于生产。

## 测试

```powershell
# 后端与 85% 覆盖率门槛
.\.venv\Scripts\python.exe -m pytest --cov=secagent --cov-report=term-missing --cov-fail-under=85

# 前端组件测试、类型检查与构建
cd frontend
npm ci
npm run test -- --run
npm run build
cd ..

# 安全回归
.\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_file_security.py backend/tests/unit/test_url_guard.py backend/tests/unit/test_tool_registry_and_risk.py -v

# 三场景浏览器验收（会启动 Docker Compose）
npm ci
npm run build:fixtures
npx playwright install chromium
npm run test:e2e

# 最小离线验收：后端、前端构建和 Secret 扫描
cd ..
powershell -ExecutionPolicy Bypass -File scripts\offline_acceptance.ps1
```

`offline_acceptance.ps1` 不调用真实模型，也不要求 API key。Docker daemon、Worker 恢复演练和付费 Provider 验收属于部署环境检查，需在 Docker 启动且本机已配置未提交 Secret 后单独执行。

## 安全限制

- 没有任意 Shell/命令执行工具；所有工具必须在注册表白名单中。
- `high` 和 `forbidden` 风险动作在 MVP 中直接拒绝；`medium` 必须人工审批。
- 上传文件进入任务独立目录；ZIP 有文件数、展开大小、符号链接和路径穿越检查。
- 源码审计只读取文本，不安装依赖、不导入模块、不运行代码。
- Web 工具仅允许 GET/POST/HEAD/OPTIONS；每次跳转前重新校验目标地址，请求体、认证头、Cookie 和敏感响应片段不会原样进入证据账本。
- 模型声明的风险级别不可信，实际审批策略始终取自后端工具注册表；当前 HTTP 工具为 `medium`，执行前必须人工批准。
- critic 判定证据不足时可带着脱敏观察重新规划，但总轮次、步骤、模型调用、Token 和任务时限都有硬上限。
- 模型输出必须通过 Pydantic 结构校验，不能绕过 RiskGate 或直接执行工具。
- Mock 是演示数据，不应当作为真实安全结论；真实环境仍需安全人员复核。

完整使用流程见 [docs/user-manual.md](docs/user-manual.md)，部署细节见 [docs/deployment.md](docs/deployment.md)，比赛演示顺序见 [docs/demo-script.md](docs/demo-script.md)。
