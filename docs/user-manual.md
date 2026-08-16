# SecAgent-X 使用与管理手册

> 版本范围：当前 MVP 实现（FastAPI + Vue 3 + PostgreSQL + Redis/Celery）。

## 1. 手册用途

这份手册同时面向两类人：

- **管理员**：负责启动服务、创建账号、配置 GLM 和 OpenCode Go/DeepSeek 凭据、查看运行状态和审计记录。
- **分析员**：负责提交有明确授权范围的安全任务，处理人工审批，查看执行过程、证据和报告。

SecAgent-X 是一个受安全边界约束的安全分析 Agent。它可以理解目标、制定步骤、调用白名单工具、根据观察结果有限重规划，并生成基于证据的报告；它不是没有限制的自动攻击程序。

使用前必须确认目标和材料属于你的授权范围。系统不会替代安全人员做最终判断，尤其是 Mock 模式或证据不完整时，报告只能作为分析辅助材料。

## 2. 已完成的工作

### 2.1 应用组成

| 部分 | 已实现内容 |
| --- | --- |
| 控制台 | Vue 3 网页控制台，提供总览、创建任务、任务中心、报告和管理员页面 |
| API | FastAPI 登录、任务、生命周期、审批、报告、系统状态和管理员 API |
| 持久化 | PostgreSQL 保存账号、任务、步骤、模型调用、工具调用、审批、证据和报告 |
| 队列 | Redis + Celery Worker 负责异步执行和任务租约 |
| 权限 | `admin` 管理员和 `analyst` 分析员两种角色 |
| Provider 凭据 | GLM 与 DeepSeek 路由的 API Key 通过管理员页面加密保存；页面只显示配置状态和尾部提示 |
| 证据账本 | 工具观察、来源、置信度和哈希写入 Evidence Ledger，报告只能引用已保存事实 |

### 2.2 三类任务

1. **日志应急响应**：识别日志格式、解析事件、匹配攻击模式并生成时间线和报告。
2. **静态源码审计**：只读扫描危险执行函数、硬编码密钥、危险配置等问题；不会安装依赖、导入模块或运行上传代码。
3. **被动 Web 分析**：在授权主机范围内执行受 URL Guard 约束的 HTTP 观察，记录状态、响应头和表单等脱敏观察，并在需要时请求人工审批。

### 2.3 模型模式

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| `mock` | 完全离线、确定性演示，不调用外部 Provider；页面和报告标记“演示结果” | 没有 API Key 的本地演示和回归测试 |
| `auto` | 按阶段使用已配置的 GLM/DeepSeek；Provider 不可用时按配置尝试回退，最后才显式降级为 Mock | 开发和演示 |
| `live` | 只使用已配置的真实 Provider；失败时不会退回 Mock | 真实分析环境 |

项目不使用 GPT。当前模型分工是：GLM 负责中文任务解析和中文报告，DeepSeek 负责技术规划和证据复核。

## 3. Agent 如何工作

### 3.1 执行流程

```text
用户目标与授权范围
  -> GLM 解析任务
  -> DeepSeek/OpenCode Go 制定受限计划
  -> 后端 RiskGate 校验并请求必要审批
  -> 白名单工具执行并写入 Evidence Ledger
  -> DeepSeek/OpenCode Go 复核证据，必要时有限重规划
  -> GLM 基于已保存证据生成中文报告
```

每一步都会保存到任务详情：当前步骤、模型调用、工具调用、审批、观察结果和证据。Agent 的“自主”只发生在计划和复核范围内，执行仍由后端白名单、风险策略和任务预算控制。

### 3.2 OpenCode Go 在系统中的位置

OpenCode Go 是一个 OpenAI-compatible 模型后端，不是远程执行器，也不会接管本项目的任务队列或工具权限。

在本项目内部，它使用 **DeepSeek 路由** 的配置项和凭据，主要参与：

- 技术步骤规划；
- 根据工具观察进行证据复核；
- 判断是否需要有限的补充规划。

实际的 HTTP、文件读取、日志解析和源码扫描仍由 SecAgent-X 后端的白名单工具执行。模型不能直接执行 Shell，也不能绕过 RiskGate 或自行批准动作。

### 3.3 有界重规划

当 critic 判断事实证据不足时，Agent 可以根据已脱敏的观察请求补充步骤，但有硬上限：

- 每个任务的最大步骤数；
- 最大重规划轮数；
- 模型调用次数和 Token 预算；
- 单任务总执行时限。

因此，任务可能因为超时、预算耗尽、Provider 不可用或证据不足而结束为失败状态，不会无限循环。

## 4. 五分钟启动

### 4.1 前置条件

- Windows PowerShell；
- Docker Desktop 已启动并允许 Compose；
- 仓库目录，例如 `F:\codex\secagent-x`；
- 生成加密 Provider 凭据所需的 Python 环境。完整 Python/Node 本地开发安装见 [README.md](../README.md)。

下面命令均从项目根目录执行。

### 4.2 初始化本地配置

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets | Out-Null
$key = & .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Set-Content -NoNewline secrets\provider_credential_encryption_key.txt $key
```

`secrets\provider_credential_encryption_key.txt` 用来加密数据库中的 Provider API Key。它是本机秘密，不要提交 Git、粘贴到聊天或写入文档。

如果本机还没有 `.venv`，先按 [README.md](../README.md) 的 Windows 本地开发步骤创建并安装依赖，再运行上面的生成命令。

### 4.3 启动 Compose

```powershell
docker compose config
docker compose up --build -d
docker compose ps
```

正常情况下打开：

```text
http://127.0.0.1:18080
```

如果端口被占用，在 `.env` 修改 `SECAGENT_PORT` 后重新启动。前端容器对外提供控制台，API、Worker、PostgreSQL、Redis 和 `web-demo` 在 Compose 网络中协同工作。

### 4.4 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:18080/api/health/live
Invoke-RestMethod http://127.0.0.1:18080/api/health/ready
Invoke-RestMethod http://127.0.0.1:18080/api/models/status
Invoke-RestMethod http://127.0.0.1:18080/api/tools
```

- `live` 表示 API 进程存活；
- `ready` 还会检查数据库、Redis 和当前模型配置；
- `models/status` 显示模式、模型和配置状态；
- `tools` 显示当前注册的白名单工具和风险级别。

在 `mock` 模式下没有 Provider Key 也可以离线运行。在 `live` 模式下，两个 Provider 凭据没有配置完成时，服务可以启动，但 readiness 会显示未就绪。

### 4.5 停止服务和数据

```powershell
docker compose down
```

普通 `down` 会停止并删除容器，但保留命名卷中的数据库和任务数据。不要使用 `docker compose down -v`，除非你明确要删除任务、证据和报告。

## 5. 登录、账号与角色

### 5.1 首次登录

本地演示默认使用：

```text
用户名：admin
密码：admin
```

这是演示账号，不是生产凭据。首次登录后应立即在 `Team` 页面为自己设置新密码，生产环境必须删除或停用这个默认组合。

如果数据库中还没有管理员账号，在 API 容器完成迁移后执行：

```powershell
docker compose exec api python -m secagent.cli create-admin --username admin
```

命令会交互式提示输入两次密码。密码长度至少为 8 个字符；不要在命令行参数、脚本、日志或截图中写入密码。

### 5.2 角色权限

| 角色 | 可以做什么 |
| --- | --- |
| 管理员 `admin` | 查看全部任务；配置 Provider Key；查看运行状态、Worker、白名单工具、成员和操作审计；新增成员、重置密码、启用或停用成员 |
| 分析员 `analyst` | 创建和查看自己的任务；开始、暂停、恢复、重试或取消允许的任务；处理自己任务的人工审批；查看自己的证据和报告 |

管理员页面在侧栏显示为 `模型与工具`、`Team` 和 `Audit`。分析员看不到这些管理员页面，也不能读取已保存的 Provider Key。

### 5.3 管理成员

管理员进入 `Team`：

1. 点击 `新增成员`；
2. 填写用户名、初始密码和角色（`分析员` 或 `管理员`）；
3. 点击 `确认`；
4. 需要改密码时，点击对应成员的 `重置密码`；
5. 停用成员时点击 `停用`，再输入成员名确认。

系统不允许停用最后一名启用中的管理员。共享账号会破坏操作审计，应为每个实际操作者创建独立账号。

## 6. 配置 GLM 与 OpenCode Go

### 6.1 先配置非秘密路由参数

编辑本地 `.env`，不要把真实 Key 写入提交文件。使用 OpenCode Go 作为 DeepSeek 路由时，最小配置如下：

```env
MODEL_MODE=live
DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_STYLE=opencode-go
DEEPSEEK_REASONING_EFFORT=high
WEB_ALLOWED_HOSTS=web-demo
```

`WEB_ALLOWED_HOSTS` 是精确主机名列表，只写主机名，不写 `https://`、路径或端口。新增目标前必须确认目标确实属于授权范围。

修改 `.env` 后重启 API 和 Worker，使非秘密配置生效：

```powershell
docker compose up -d --build api worker
```

### 6.2 在网页保存 API Key

API Key 的正常录入路径是管理员网页表单：

1. 登录控制台；
2. 打开侧栏 `模型与工具`；
3. 找到 `Provider 密钥` 区域；
4. 在 `deepseek` 行输入 **OpenCode Go 的 API Key**；
5. 点击该行的保存图标；
6. 在 `glm` 行输入智谱 GLM API Key，再点击保存图标；
7. 回到上方 Provider 状态，分别点击 `连通性检查`。

这里的 `deepseek` 行是项目内部的路由名称。即使实际后端是 OpenCode Go，也必须把它填在 `deepseek` 行，而不是新增一个 Provider。

保存成功后：

- API Key 会使用 `provider_credential_encryption_key` 加密后保存到数据库；
- 页面只显示“已配置”和脱敏的尾部提示，不返回完整 Key；
- 输入框保存后会被清空；
- 清除 Key 需要点击删除图标并确认；
- 连通性检查会显示状态、请求 ID、Token 摘要和延迟，但不会展示完整凭据。

不要把 Key 放在任务目标、授权范围、Web URL、报告、截图、日志或 Git 提交中。不要用带有 `user:password@host` 的 URL 传递认证信息。

### 6.3 Provider 状态如何判断

- `已配置` 只说明加密凭据存在，不等于 Provider 一定可用；
- `连通性检查` 成功后，才说明当前 endpoint、模型和凭据组合能完成一次检查；
- `live` 模式下 readiness 未就绪，先检查两行凭据、Base URL、模型名、网络和容器日志；
- `auto` 模式可能回退到另一个已配置 Provider 或 Mock，任务详情中的模型调用记录是最终依据；
- `mock` 模式不访问 OpenCode Go 或 GLM，报告会带“演示结果”标记。

## 7. 创建并执行任务

### 7.1 创建页面字段

打开 `创建任务`，填写以下字段：

| 页面字段 | 填写原则 |
| --- | --- |
| `任务目标` | 写清楚要分析什么、要回答什么问题、需要什么输出 |
| `授权范围` | 写清允许访问的材料、主机、方法和禁止事项；这是工具执行的边界 |
| `场景` | 可选自动识别，也可以选择日志应急响应、静态源码审计或被动 Web 分析 |
| `模型路由` | `自动协同` 使用阶段路由；`手动指定` 后再选择 DeepSeek 或 GLM |
| `授权 URL（Web 场景）` | Web 任务填写完整 HTTP(S) URL；必须同时满足授权和 URL Guard |
| 上传材料 | 日志、源码文本或受限制的 ZIP；上传文件进入任务独立工作区 |

授权范围不要只写“允许测试”。应写成可检查的边界，例如“只读取本次上传的日志，不执行外部命令”，或“只访问 `http://web-demo/` 的 GET 观察，不提交表单”。

### 7.2 日志应急响应

示例：

```text
任务目标：分析上传的 access.log，识别异常扫描并生成证据报告。
授权范围：仅分析本次上传的日志，不访问外部网络，不执行任何命令。
场景：日志应急响应
材料：access.log
```

提交后进入任务详情，点击 `开始执行`。执行时间线通常会展示格式识别、日志解析、攻击模式和时间线等步骤。证据来源应能定位到文件和行号，例如日志文件的具体行。

### 7.3 静态源码审计

示例：

```text
任务目标：审计上传源码中的危险执行、硬编码密钥和不安全配置。
授权范围：只读审计上传的 ZIP；不安装依赖、不导入模块、不运行源码。
场景：静态源码审计
材料：源码 ZIP
```

系统会在独立目录中安全解压，并检查路径穿越、符号链接、文件数和展开大小。源码只作为文本读取；发现的密钥值会脱敏为 `***REDACTED***` 一类形式。

### 7.4 被动 Web 分析

示例：

```text
任务目标：对授权站点做被动 Web 分析，记录响应和公开表单结构，不提交表单。
授权范围：只访问 http://web-demo/，允许 GET 观察，不提交表单、不携带认证信息。
场景：被动 Web 分析
授权 URL：http://web-demo/
```

Web 工具目前只允许注册的 HTTP 方法（GET、POST、HEAD、OPTIONS），每个目标和每次重定向都会重新经过 URL Guard。请求和响应会限长并脱敏后再写入证据账本。

### 7.5 开始、暂停和观察

创建任务后状态为 `created`。在任务详情点击 `开始执行`：

1. 任务进入队列，由 Worker 领取；
2. 页面显示队列位置、尝试次数、当前阶段和 Worker 心跳；
3. 左侧 `执行时间线` 展示步骤；
4. 右侧 `证据账本` 实时更新；
5. 若命中中风险工具，任务会停在 `waiting_human`，等待人工处理。

执行期间可以点击 `暂停`。暂停后点击 `恢复`，系统会从允许恢复的位置继续。不要通过重复刷新或重复点击创建多个任务；按钮会按请求状态防止重复提交。

## 8. 人工审批 Web 操作

### 8.1 什么时候会等待审批

当前 HTTP 工具注册为 `medium` 风险，涉及 Web 操作时可能进入人工审批。高风险或禁止动作在 MVP 中直接拒绝；模型声明的风险等级不能降低后端工具注册表的实际风险。

### 8.2 审批步骤

任务显示 `waiting_human` 或出现 `查看待审批动作` 后：

1. 点击 `查看待审批动作`；
2. 核对 `工具` 是否为预期的白名单工具；
3. 核对 `风险`；
4. 核对完整的 `参数/目标`，确认主机、路径、方法和授权范围一致；
5. 填写审批理由；
6. 确认无误时点击 `批准并继续`，否则点击 `拒绝`。

批准只对当前工具步骤和当前参数生效。步骤被重置、目标或参数发生变化时，原审批会失效，需要重新核对。拒绝后任务不会继续执行该动作，通常会进入取消或终止流程。

### 8.3 审批判断原则

批准前至少确认：

- 目标属于书面授权范围；
- URL 没有认证信息、回环地址、私网地址或云元数据地址，除非是显式授权的演示主机；
- HTTP 方法与任务目标一致；
- 不会提交表单、上传文件或改变目标状态；
- 参数没有混入 API Key、Cookie、密码或 Bearer Token。

## 9. 查看执行过程、证据与报告

### 9.1 执行时间线

任务详情左侧的 `执行时间线` 展示每一步的名称、目的、模型路由、工具、风险和状态。它回答“Agent 计划了什么、执行到哪一步、哪一步需要人确认”。

### 9.2 证据账本

右侧 `证据账本` 的 `证据` 标签是事实来源。每条记录通常包含：

- `evidence_type`：证据类型；
- `source`：文件、工具或响应来源；
- `content`：经过限长和脱敏的内容；
- `confidence`：证据置信度；
- `evidence_hash`：用于一致性校验的哈希。

报告中的结论应该能回到这里。模型的自然语言解释不是独立事实，不能替代源文件、工具观察或行级证据。

### 9.3 工具和模型标签

- `工具`：查看白名单工具调用、参数摘要、执行状态和脱敏结果；
- `模型`：查看 Provider、模型、阶段、路由原因、延迟、Token 摘要和请求 ID；
- `演示结果`：说明该任务使用了 Mock 或包含 Mock 回退，不能当作真实环境结论。

### 9.4 查看报告

任务完成后，在任务详情点击 `查看报告`，或打开侧栏 `报告` 并选择任务。报告以 Markdown/纯文本形式展示，事实依据以该任务的 Evidence Ledger 为准。

建议先看证据，再看报告：

1. 确认来源和授权范围；
2. 检查是否存在脱敏、超时或工具警告；
3. 对照执行时间线确认报告没有引用未执行的步骤；
4. 再将报告交给安全人员复核和处置。

## 10. 任务状态与操作

| 状态 | 含义 | 页面上的常见操作 |
| --- | --- | --- |
| `created` | 任务已创建，尚未进入执行队列 | 开始执行、取消 |
| `queued` | 已排队等待 Worker | 等待或取消 |
| `parsed` | 任务已完成场景和目标解析 | 等待后续计划 |
| `planned` | Agent 已形成当前计划 | 等待执行 |
| `running` | Worker 正在执行 | 暂停、取消 |
| `waiting_human` | 某个工具动作等待人工审批 | 查看待审批动作、批准、拒绝、取消 |
| `paused` | 操作者暂停了任务 | 恢复、取消 |
| `completed` | 任务完成并生成结果 | 查看证据和报告 |
| `failed_retryable` | 运行失败但允许重新排队 | 重试、取消 |
| `failed` | 任务不可重试地失败 | 查看错误和证据 |
| `cancelled` | 任务被操作者或审批流程取消 | 查看已保存记录 |

API 或 Worker 异常退出后，启动恢复逻辑会把仍处于运行中的任务标记为 `failed_retryable`。重试前应先查看 `runtime_error` 和已有证据，确认授权仍然有效。

## 11. 安全边界与使用限制

### 11.1 工具和网络

- 没有任意 Shell 或命令执行工具；模型只能请求注册表中的白名单工具。
- 高风险和 `forbidden` 动作不会执行；`medium` 动作按现有 RiskGate 进入人工审批。
- Web 目标必须是 HTTP(S)，并通过精确主机白名单和地址解析检查。
- 回环、私网、链路本地和云元数据地址默认阻断；显式授权的演示主机可加入 `WEB_ALLOWED_HOSTS`。
- 每次重定向都重新检查目标，不能通过重定向绕过第一跳的授权。
- 请求头、Cookie、请求体、敏感响应片段和 Provider 原始响应会在落盘前脱敏或截断。

### 11.2 文件和源码

- 上传文件进入任务独立工作区；ZIP 有文件数和展开总量限制。
- 路径穿越和符号链接等危险归档会被拒绝。
- 源码审计只读取文本，不安装依赖、不导入代码、不执行程序。
- 上传材料不应包含不必要的密码、私钥、生产 Token 或个人数据。

### 11.3 模型和数据

- 所有模型输出都要通过结构校验，不能直接绕过 RiskGate 执行工具。
- critic 只能基于已保存且脱敏的观察判断证据是否充分。
- 重规划、步骤、模型调用、Token 和总时限都有硬上限。
- Mock 结果是演示数据，不应作为真实安全结论。
- Provider Key 使用数据库加密存储；管理员页面不会显示完整密钥，但仍应保护管理员会话和加密主密钥。

### 11.4 部署建议

本地演示可使用 `admin/admin` 和 `COOKIE_SECURE=false`。共享环境或生产部署至少要：

1. 更换演示管理员密码和 JWT 签名密钥；
2. 使用独立的 Provider 加密主密钥，并限制文件权限；
3. 通过 HTTPS 访问控制台；
4. 使用 Docker Secrets 或等价的安全密钥注入；
5. 配置最小化的 Web 主机白名单；
6. 备份 PostgreSQL、Redis 和证据数据卷；
7. 让安全人员复核每个授权范围和最终报告。

生产配置、备份、迁移和回滚细节见 [部署与运维文档](deployment.md)。

## 12. 常见问题与排障

| 现象 | 可能原因 | 排查方式 |
| --- | --- | --- |
| 浏览器打不开 `18080` | 容器未启动或端口冲突 | `docker compose ps`；检查 `.env` 的 `SECAGENT_PORT`；查看 frontend 日志 |
| `live` 正常但 `ready` 未就绪 | 数据库、Redis 或 live Provider 配置不完整 | 查看 `/api/health/ready` 返回的具体检查项，再看 API 日志 |
| Provider 显示未配置 | Key 尚未通过管理员页面保存，或加密主密钥缺失 | 检查 `模型与工具`、`secrets\provider_credential_encryption_key.txt` 和容器挂载，不要打印 Key |
| 连通性检查失败 | Base URL、模型名、Key、网络或 Provider 返回错误 | 检查非秘密配置和 `/api/models/status`，再查看 API 日志中的错误码 |
| 创建后任务没有推进 | Worker 未在线、队列阻塞或任务仍在排队 | 查看 `docker compose ps`、Worker 日志、任务的队列位置和心跳 |
| 任务停在 `waiting_human` | 正在等待中风险工具审批 | 打开 `查看待审批动作`，核对目标后批准或拒绝；这是预期流程 |
| Web 目标被拒绝 | 未加入精确白名单、解析到私网/回环、URL 含认证信息或方法不允许 | 修正授权范围和白名单；不要通过绕过 URL Guard 的方式解决 |
| 上传 ZIP 被拒绝 | 路径穿越、符号链接、文件数或展开大小超限 | 清理归档结构，重新打包；不要放大限制来接受未知文件 |
| 状态为 `failed_retryable` | Worker、模型或 HTTP 传输错误等可重试故障 | 先查看错误证据和授权，再点击 `重试` |
| 没有报告 | 任务未完成、被取消，或证据不足导致失败 | 先看状态、时间线、模型和证据标签；不要把模型草稿当正式报告 |
| 页面显示演示结果 | 当前任务使用 Mock 或发生 Mock 回退 | 切到 `live` 并配置两个 Provider 后重新创建任务 |

常用日志命令：

```powershell
docker compose logs --tail 200 api
docker compose logs --tail 200 worker
docker compose logs --tail 200 frontend
docker compose logs --tail 100 web-demo
```

日志中不应出现完整 API Key、密码、JWT 或 Cookie。如果发现敏感信息，先停止共享日志并轮换对应凭据。

## 13. 常用运维命令

### 13.1 启停和检查

```powershell
docker compose config
docker compose up --build -d
docker compose ps
docker compose restart api worker frontend
docker compose down
```

### 13.2 API 快速参考

网页已经封装了这些接口，通常不需要手写请求。调试或接入其他前端时，可参考：

| 用途 | 方法和路径 |
| --- | --- |
| 登录 | `POST /api/auth/login` |
| 创建任务 | `POST /api/tasks` |
| 任务列表和详情 | `GET /api/tasks`、`GET /api/tasks/{id}` |
| 任务生命周期 | `POST /api/tasks/{id}/run`、`pause`、`resume`、`retry`、`cancel` |
| 人工审批 | `POST /api/tasks/{id}/approve` |
| 报告 | `GET /api/tasks/{id}/report` |
| 健康和状态 | `GET /api/health/live`、`/api/health/ready`、`/api/models/status`、`/api/tools` |
| Provider 管理员操作 | `GET/PUT/DELETE /api/admin/provider-credentials...` |

生命周期和审批请求应携带登录会话及幂等键；直接调用 API 时不要把凭据放入 URL。接口的权限和参数校验仍由后端执行，不能因为绕过网页就获得额外权限。

### 13.3 数据备份和生产部署

备份命名卷、SQLite 迁移、生产 Docker Secret、升级和回滚请按 [docs/deployment.md](deployment.md) 执行。恢复前先停止 API/Worker，并保留当前数据卷的额外备份。

## 14. 验收结果与相关文档

当前版本已完成以下验收：

- 后端测试：273 项通过，覆盖率 91.11%；
- 前端测试：41 项通过；
- TypeScript 检查和 Vite 生产构建通过；
- Docker Compose 的 API、PostgreSQL、Redis 和 Worker 健康检查通过；
- 完成一次真实 Web Agent 闭环：任务执行、1 次人工审批、4 次模型调用、4 次工具调用、6 条证据和 1 份报告。

相关文档：

- [项目 README](../README.md)：功能概览、本地开发和测试命令；
- [部署与运维](deployment.md)：生产覆盖、备份恢复、迁移和回滚；
- [演示脚本](demo-script.md)：日志、源码和 Web 三场景的讲解顺序；
- [Web Agent 设计说明](superpowers/specs/2026-08-15-web-task-agent-design.md)：Web 任务 Agent 的边界和数据流；
- [Provider Key 设计说明](superpowers/specs/2026-08-15-provider-key-management-design.md)：凭据加密和管理流程。

最后提醒：授权范围由任务创建者负责，模型输出由系统负责结构化和限权，但最终安全结论、处置动作和对外报告仍必须由有权限的安全人员复核。


