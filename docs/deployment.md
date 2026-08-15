# SecAgent-X 部署与运维

## 环境变量

从 `.env.example` 复制为 `.env`。`.env` 已被 Git 忽略，不得提交真实密钥。

- `MODEL_MODE`：`mock`、`auto` 或 `live`；`SECAGENT_PORT` 是宿主机前端端口，默认 `18080`。
- `DEEPSEEK_API_KEY/BASE_URL/MODEL`：DeepSeek OpenAI-compatible 接口。
- `GLM_API_KEY/BASE_URL/MODEL`：智谱 GLM OpenAI-compatible 接口。
- `DATABASE_URL`：开发 Compose 默认指向 `postgres:16`。
- `REDIS_URL`：开发 Compose 默认指向 `redis:7`。
- `DATA_DIR`：任务工作区、上传材料所在根目录。
- `WORKER_CONCURRENCY`：Worker 并发，Settings 强制范围为 1-3。
- `UPLOAD_MAX_BYTES`：单文件大小上限。
- `ARCHIVE_MAX_FILES/ARCHIVE_MAX_BYTES`：ZIP 文件数和展开总量上限。
- `WEB_ALLOWED_HOSTS`：逗号分隔的精确主机名白名单，只用于明确授权的私网演示主机。
- `MAX_STEPS_PER_TASK/MAX_REPLANS/TASK_TIMEOUT_SECONDS`：操作员预算参数；当前 MVP 使用单计划闭环，保留这些值用于后续调度强化。

## 启动与健康检查

```powershell
Copy-Item .env.example .env
docker compose config
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:18080/api/health/live
Invoke-RestMethod http://127.0.0.1:18080/api/health/ready
Invoke-RestMethod http://127.0.0.1:18080/api/models/status
Invoke-RestMethod http://127.0.0.1:18080/api/tools
```

开发栈显式使用 `MODEL_MODE=mock`，API 启动前执行 Alembic 迁移；PostgreSQL 和 Redis 健康后 API 才启动，Worker 和前端等待 API readiness。`web-demo` 不映射到宿主机端口，只能从 Compose 内部网络访问。

生产覆盖使用三个只读 Secret。先在 Windows 主机创建文件（内容只保存在本机）：

```powershell
Copy-Item secrets\deepseek_api_key.txt.example secrets\deepseek_api_key.txt
Copy-Item secrets\glm_api_key.txt.example secrets\glm_api_key.txt
Copy-Item secrets\jwt_signing_key.txt.example secrets\jwt_signing_key.txt
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

生产覆盖强制 `MODEL_MODE=live`、`COOKIE_SECURE=true` 和最多 3 个 Worker 并发；不会在 readiness 中调用付费 Provider，只验证两个 Provider 配置和 JWT Secret 存在。

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

升级前备份 `secagent-data`，执行 `docker compose build --pull`，再运行完整测试矩阵。回滚时使用之前的镜像/代码版本并恢复兼容的数据卷备份。当前 MVP 使用 `create_all` 管理初始表结构，生产升级到多版本 schema 前应引入 Alembic 迁移。
