# SecAgent-X 部署与运维

## 环境变量

从 `.env.example` 复制为 `.env`。`.env` 已被 Git 忽略，不得提交真实密钥。

- `MODEL_MODE`：`mock`、`auto` 或 `live`；`SECAGENT_PORT` 是宿主机前端端口，默认 `18080`。
- `DEEPSEEK_API_KEY/BASE_URL/MODEL`：DeepSeek 路由使用的 OpenAI-compatible 接口。
- `DEEPSEEK_API_STYLE`：`auto`、`deepseek` 或 `opencode-go`；OpenCode Go 使用 `reasoning_effort`，官方 DeepSeek 使用 `thinking`。
- `DEEPSEEK_REASONING_EFFORT`：OpenCode Go 推理强度，默认 `high`。
- `GLM_API_KEY/BASE_URL/MODEL`：智谱 GLM OpenAI-compatible 接口。
- `PROVIDER_CREDENTIAL_ENCRYPTION_KEY/PROVIDER_CREDENTIAL_ENCRYPTION_KEY_FILE`：用于加密数据库中的 Provider 凭据；部署时必须提供，文件设置优先于环境变量。
- `DATABASE_URL`：开发 Compose 默认指向 `postgres:16`。
- `REDIS_URL`：开发 Compose 默认指向 `redis:7`。
- `DATA_DIR`：任务工作区、上传材料所在根目录。
- `WORKER_CONCURRENCY`：Worker 并发，Settings 强制范围为 1-3。
- `UPLOAD_MAX_BYTES`：单文件大小上限。
- `ARCHIVE_MAX_FILES/ARCHIVE_MAX_BYTES`：ZIP 文件数和展开总量上限。
- `WEB_ALLOWED_HOSTS`：逗号分隔的精确主机名白名单，只用于明确授权的私网演示主机。
- `MAX_STEPS_PER_TASK/MAX_REPLANS/TASK_TIMEOUT_SECONDS`：Agent 的步骤、补充规划轮次和总执行时限。

OpenCode Go 的推荐最小配置如下。真实 API key 通过管理员网页表单录入，不要放入文档或提交的配置文件：

```env
MODEL_MODE=live
DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_STYLE=opencode-go
DEEPSEEK_REASONING_EFFORT=high
WEB_ALLOWED_HOSTS=web-demo,node4.anna.nssctf.cn
```

`WEB_ALLOWED_HOSTS` 只写精确主机名，不带 scheme、路径或端口。明确授权的私网演示主机可通过白名单访问；其他主机仍必须解析为全局可路由地址。HTTP 请求仅允许 GET/POST/HEAD/OPTIONS，所有请求均按 `medium` 风险进入现有人工审批流程，且每个重定向目标都会重新检查。

## 启动与健康检查

```powershell
Copy-Item .env.example .env
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
docker compose config
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:18080/api/health/live
Invoke-RestMethod http://127.0.0.1:18080/api/health/ready
Invoke-RestMethod http://127.0.0.1:18080/api/models/status
Invoke-RestMethod http://127.0.0.1:18080/api/tools
```

`secrets\provider_credential_encryption_key.txt` 是本地生成且被 Git 忽略的文件；必须在第一次 `docker compose up` 前创建，基础 Compose 会以只读方式将它挂载到 API 和 Worker 的同一路径。不要把值写入 `.env`、示例文件或提交记录。

开发栈默认使用 `MODEL_MODE=mock`，也可通过 `.env` 切换到 `live`；API 启动前执行 Alembic 迁移，PostgreSQL 和 Redis 健康后 API 才启动，Worker 和前端等待 API liveness。`web-demo` 不映射到宿主机端口，只能从 Compose 内部网络访问。`/api/health/live` 只表示进程存活；在 `MODEL_MODE=live` 下，即使两个数据库 Provider 凭据尚未保存，控制台、Worker 和前端也会启动，而 `/api/health/ready` 会返回 not-ready 且 `model_configuration` 为 failed。

生产覆盖使用四个只读 Docker Secret，其中加密主密钥与本地使用同名的 `provider_credential_encryption_key` Secret。先在 Windows 主机创建文件（内容只保存在本机）：

```powershell
Copy-Item secrets\deepseek_api_key.txt.example secrets\deepseek_api_key.txt
Copy-Item secrets\glm_api_key.txt.example secrets\glm_api_key.txt
Copy-Item secrets\jwt_signing_key.txt.example secrets\jwt_signing_key.txt
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

生产覆盖强制 `MODEL_MODE=live`、`COOKIE_SECURE=true` 和最多 3 个 Worker 并发；不会在 readiness 中调用付费 Provider，只验证两个 Provider 配置和 JWT Secret 存在。DeepSeek/GLM 环境变量和文件 Secret 仍是向后兼容的回退方式；正常的 Provider key 录入路径是管理员登录控制台后的浏览器表单，密钥会加密保存在数据库中。除本地测试外，必须通过 HTTPS 部署控制台以保护管理员会话和录入的凭据。

## 管理员初始化

API 完成迁移后，创建本地演示管理员：

```powershell
docker compose exec api python -m secagent.cli create-admin --username admin
```

在两次密码提示中输入 `admin`，即可创建 `admin/admin`。这只用于本地演示，首次登录后必须立即更改该密码；不要在共享或生产环境使用这个密码。

## 数据卷备份与恢复

列出卷：

```powershell
docker volume ls | Select-String secagent
docker compose stop api worker
docker run --rm -v secagent-x_secagent-data:/data -v ${PWD}:/backup alpine tar czf /backup/secagent-data.tgz -C /data .
docker compose start api worker
```

恢复前先停止后端，并将备份解压回同一命名卷。卷名可能带 Compose 项目前缀，请以 `docker volume ls` 的实际结果为准。恢复操作会覆盖目标数据，执行前应另做一份当前卷备份。

## SQLite 迁移到 PostgreSQL

旧版 MVP 的 SQLite 数据需要按部署窗口单独迁移；当前 Compose 拓扑直接使用 PostgreSQL，启动 API 会执行 Alembic migration。

```powershell
$env:TARGET_DATABASE_URL = "postgresql+psycopg://<user>:<password>@<host>/<database>"
python scripts\migrate_sqlite_to_postgres.py `
  --source "sqlite:////data/secagent.db" `
  --target $env:TARGET_DATABASE_URL `
  --owner-username migration-owner
```

确认 dry-run 的每张表 source/target 统计、插入数、跳过数和 evidence hash 校验结果后，增加 `--apply` 才会在一个事务中写入：

```powershell
python scripts\migrate_sqlite_to_postgres.py `
  --source "sqlite:////data/secagent.db" `
  --target $env:TARGET_DATABASE_URL `
  --owner-username migration-owner `
  --apply
```

迁移前同时备份 SQLite 数据卷和 PostgreSQL 目标库，例如使用 `pg_dump` 保存目标快照。脚本会按任务、步骤、模型调用、工具调用、证据、审批、报告的依赖顺序写入；缺失证据 hash 会按内容计算 SHA-256，已有 hash 必须匹配。任何关系、计数或 hash 校验失败都会使事务回滚并返回非零退出码。重复执行 `--apply` 会按主键和内容校验并跳过已导入行，不产生重复记录。

失败后先保留命令输出和数据库备份；目标事务失败时无需手工清理。若需要恢复已提交的迁移，停止应用连接后从迁移前的 PostgreSQL 备份恢复，再按上面的 dry-run 流程重新核对。不要把带密码的数据库 URL 写入脚本、日志或提交记录；脚本 JSON 输出不包含数据库 URL、证据内容或 API key。

## 中断任务恢复

后端启动时会把上次异常退出时仍为 `running` 的任务改为 `failed_retryable`。操作员在任务详情核对证据和授权范围后点击“重试”；终态任务不会自动重跑。

## 日志与故障定位

浏览器通过 `/api/conversations/{id}/events?ticket=...` 建立对话事件流。应用会在 Uvicorn access logger 中把所有 percent-decoded 名称恰为 `ticket` 的查询参数值替换为 `[REDACTED]`，但它无法配置外部组件。生产部署必须对每一层上游反向代理、负载均衡器、Ingress、CDN 和 access logger 禁止记录该事件流路由，或实施同等的完整 `ticket` 查询值净化（包括重复参数和 `%74icket` 等编码名称）。不得把原始 stream-ticket JWT 或 JTI 写入持久日志、审计记录或追踪系统；仅记录不含凭据的路由、状态码和应用 trace ID。

```powershell
docker compose logs --tail 200 api
docker compose logs --tail 200 worker
docker compose logs --tail 200 frontend
docker compose logs --tail 100 web-demo
```

- 模型失败：检查模式、密钥、Base URL 和容器网络；`live` 不会退回 Mock。
- Web 被拒绝：检查目标是否为 HTTP(S)、是否包含认证信息、解析地址是否为私网，以及是否确属授权白名单。
- ZIP 被拒绝：检查路径穿越、符号链接、文件数量和展开大小。
- 任务 `failed_retryable`：先查看 Evidence Ledger 中的 `runtime_error`，再决定重试或取消。

## 升级与回滚

升级前备份 PostgreSQL、Redis 和证据卷，执行 `docker compose build --pull`，再运行离线验收与部署环境检查。回滚时使用之前的镜像/代码版本并恢复兼容的数据卷备份。当前服务启动前由 Alembic 管理 schema；`create_all` 仅用于 SQLite 测试 fixture。

## 离线验收

在没有 Docker daemon 或真实模型密钥的开发机上，运行以下命令验证后端覆盖率、前端测试/构建和 Secret 扫描：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\offline_acceptance.ps1
```

该命令不会启动容器或调用 Provider。Docker Compose 健康状态、Worker 异常恢复和 live Provider 冒烟需在部署环境中单独执行。
