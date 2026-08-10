# SecAgent-X 部署与运维

## 环境变量

从 `.env.example` 复制为 `.env`。`.env` 已被 Git 忽略，不得提交真实密钥。

- `MODEL_MODE`：`mock`、`auto` 或 `live`；`SECAGENT_PORT` 是宿主机前端端口，默认 `18080`。
- `DEEPSEEK_API_KEY/BASE_URL/MODEL`：DeepSeek OpenAI-compatible 接口。
- `GLM_API_KEY/BASE_URL/MODEL`：智谱 GLM OpenAI-compatible 接口。
- `DATABASE_URL`：Docker 默认 `sqlite:////data/secagent.db`。
- `DATA_DIR`：任务工作区、上传材料和 SQLite 所在根目录。
- `UPLOAD_MAX_BYTES`：单文件大小上限。
- `ARCHIVE_MAX_FILES/ARCHIVE_MAX_BYTES`：ZIP 文件数和展开总量上限。
- `WEB_ALLOWED_HOSTS`：逗号分隔的精确主机名白名单，只用于明确授权的私网演示主机。
- `MAX_STEPS_PER_TASK/MAX_REPLANS/TASK_TIMEOUT_SECONDS`：操作员预算参数；当前 MVP 使用单计划闭环，保留这些值用于后续调度强化。

## 启动与健康检查

```powershell
Copy-Item .env.example .env
python scripts\build_demo_archives.py
docker compose config
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:18080/api/health
Invoke-RestMethod http://127.0.0.1:18080/api/models/status
Invoke-RestMethod http://127.0.0.1:18080/api/tools
```

后端健康检查成功后前端才启动。`web-demo` 不映射到宿主机端口，只能从 Compose 内部网络访问。

## 数据卷备份与恢复

列出卷：

```powershell
docker volume ls | Select-String secagent
docker compose stop backend
docker run --rm -v secagent-x_secagent-data:/data -v ${PWD}:/backup alpine tar czf /backup/secagent-data.tgz -C /data .
docker compose start backend
```

恢复前先停止后端，并将备份解压回同一命名卷。卷名可能带 Compose 项目前缀，请以 `docker volume ls` 的实际结果为准。恢复操作会覆盖目标数据，执行前应另做一份当前卷备份。

## 中断任务恢复

后端启动时会把上次异常退出时仍为 `running` 的任务改为 `failed_retryable`。操作员在任务详情核对证据和授权范围后点击“重试”；终态任务不会自动重跑。

## 日志与故障定位

```powershell
docker compose logs --tail 200 backend
docker compose logs --tail 200 frontend
docker compose logs --tail 100 web-demo
```

- 模型失败：检查模式、密钥、Base URL 和容器网络；`live` 不会退回 Mock。
- Web 被拒绝：检查目标是否为 HTTP(S)、是否包含认证信息、解析地址是否为私网，以及是否确属授权白名单。
- ZIP 被拒绝：检查路径穿越、符号链接、文件数量和展开大小。
- 任务 `failed_retryable`：先查看 Evidence Ledger 中的 `runtime_error`，再决定重试或取消。

## 升级与回滚

升级前备份 `secagent-data`，执行 `docker compose build --pull`，再运行完整测试矩阵。回滚时使用之前的镜像/代码版本并恢复兼容的数据卷备份。当前 MVP 使用 `create_all` 管理初始表结构，生产升级到多版本 schema 前应引入 Alembic 迁移。
