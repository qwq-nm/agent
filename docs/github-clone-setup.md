# SecAgent-X GitHub 克隆与本地配置说明

这份文档给团队成员使用：从 GitHub 拉取项目后，如何用 Docker 启动 SecAgent-X，并配置自己的 DeepSeek / GLM API Key。

本项目推荐统一使用 Docker 运行。这样每个人不需要在自己电脑上分别安装后端依赖、前端依赖、外部安全工具和浏览器运行环境，Docker 镜像会在构建时统一准备好这些内容。

## 1. 别人看到的功能是否和你一样

如果你已经把最新代码提交并推送到 GitHub，那么别人 clone 下来后，系统功能和页面逻辑会和你一样，包括：

- 创建任务页面
- CTF Web / 日志分析 / 源码审计等任务模式
- 执行计划展示
- 当前执行状态
- AI 决策与工具协同展示
- 证据账本
- 报告页面
- 工具插件，例如 `http_fetch`、`header_check`、`dirsearch_scan`、`browser_snapshot`、`sqlmap_probe` 等
- 人工确认流程
- 任务继续分析流程

但是以下内容不会跟着 GitHub 一起过去：

- 你的 DeepSeek API Key
- 你的 GLM API Key
- 你本地创建的账号和密码
- 你本地跑过的任务记录
- 你本地生成的报告、证据账本、数据库内容
- 你的 `.env`
- 你的 `secrets/` 真实密钥文件
- 你的 `x.db`、`data/`、Docker 运行数据
- 你的 `.venv/`、`node_modules/`、`frontend/dist/`

这是正常且必须的。别人需要在自己的电脑上重新配置账号和 API Key。

## 2. 当前系统已有内容和实际效果

SecAgent-X 目前是一个面向比赛展示的“网络安全自主决策智能体平台原型”，重点不是单纯做一个 CTF 自动解题脚本，而是展示一个完整的智能体系统框架：

- 用户用自然语言创建安全分析任务；
- AI 理解任务目标、授权范围和安全策略；
- AI 生成执行计划；
- 系统按照计划调用白名单工具；
- 工具输出写入证据账本；
- AI 根据证据复核是否需要继续分析；
- 页面展示执行计划、当前状态、工具调用、证据链和报告；
- 中高风险工具调用需要人工确认；
- 报告中的结论尽量能回溯到证据。

目前已经具备的模块包括：

| 模块 | 当前效果 |
|---|---|
| 任务创建 | 支持自然语言输入目标，也支持上传附件和填写 URL |
| 场景识别 | 可以识别 Web 分析、CTF Web、日志分析、源码审计等方向 |
| 模型协同 | 支持 GLM 和 DeepSeek；GLM 偏任务理解，DeepSeek 偏计划生成、证据复核和报告 |
| 执行计划 | 能生成并展示执行步骤，后续继续分析时可以追加计划 |
| 工具插件 | 已接入 HTTP 页面获取、响应头检查、链接提取、表单提取、robots.txt 分析、Flag 模式识别、目录扫描、浏览器快照、SQLMap 探测等工具 |
| 人工确认 | 对目录扫描、登录尝试、SQL 注入探测等风险较高动作进行人工审批 |
| 证据账本 | 保存工具输出、URL、响应状态、关键结果、置信度和哈希 |
| 运行记忆 | 记录已访问 URL、发现的链接、表单、Cookie、候选路径和候选 Flag |
| 报告生成 | 可以生成 Markdown 报告，并在页面中展示 |
| 审计与可解释性 | 页面能看到 AI 做了什么、工具做了什么、为什么调用工具、工具返回了什么 |

需要注意：当前版本还不能保证自动解出一道完整 CTF Web 题。

目前更准确的效果是：

- 能自动访问授权 URL；
- 能抓取首页、响应头、公开链接、表单、robots.txt；
- 能发现一些明显的 flag 字符串、隐藏路径、敏感文件线索；
- 能用 dirsearch 做目录发现；
- 能用 browser_snapshot 处理一部分 JS 渲染页面；
- 能在人工确认后尝试 sqlmap_probe、弱口令或万能密码探测；
- 能把每一步结果记录到证据账本；
- 能生成阶段性分析报告。

当前不足也要明确写出来，方便团队继续分工：

- AI 还不够会“像人一样解题”，很多时候只是围绕已有工具继续分析；
- 对登录、注册、验证码、动态 JS、复杂 Cookie / Session 的处理还比较弱；
- 工具结果虽然会进入证据链，但 AI 对结果的深度利用还需要继续增强；
- Web CTF 常见的业务逻辑绕过、参数构造、源码反推、复杂注入等能力还不完整；
- 现在更适合展示“自主决策智能体框架”，不适合宣传为“稳定自动解题系统”。

因此比赛展示时建议这样表述：

```text
本系统已经实现了面向网络安全任务的自主决策智能体框架，包括任务理解、计划生成、工具编排、证据账本、人机协同、风险控制和报告生成。当前版本可对授权 Web / CTF 页面进行自动化信息收集和阶段性分析，但复杂 CTF 题目的完整求解仍需要继续扩展工具链和增强模型基于证据的重规划能力。
```

## 3. 克隆项目

```powershell
git clone <你的 GitHub 仓库地址>
cd agent-feature-secagent-x-mvp
```

如果项目目录名不同，就进入实际的项目目录。

推荐运行方式：

```text
Docker Desktop + docker compose
```

不推荐每个同学都手动安装 Python 虚拟环境、Node 依赖、dirsearch、sqlmap、Playwright 浏览器。因为这样容易出现版本不一致、路径不一致、工具缺失等问题，最后导致某些人能跑、某些人跑不起来。

## 4. 准备本地配置文件

复制示例环境变量文件：

```powershell
Copy-Item .env.example .env
```

`.env` 是本地配置文件，不会提交到 GitHub。

如果只是演示界面，可以先使用：

```env
MODEL_MODE=mock
```

如果要真正调用 DeepSeek / GLM，让智能体自动分析任务，应改为：

```env
MODEL_MODE=live
```

也可以使用：

```env
MODEL_MODE=auto
```

`auto` 会优先使用已配置的真实模型，模型不可用时尝试降级处理。比赛演示如果要体现真实模型能力，建议使用 `live`。

## 5. 生成本地加密密钥

系统会把用户在网页里填写的 Provider API Key 加密保存到本地数据库，因此每台电脑都需要自己的加密密钥。

使用 Docker 启动前，先确保 `secrets/` 目录存在：

```powershell
New-Item -ItemType Directory -Force secrets
```

生成 Provider 凭据加密密钥：

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Content -NoNewline secrets\provider_credential_encryption_key.txt
```

这个文件只属于本机，不要提交到 GitHub。

如果这里提示没有 `cryptography`，不要为了这个单独配置一套开发环境。可以用 Docker 里的 Python 来生成：

```powershell
docker compose run --rm api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Content -NoNewline secrets\provider_credential_encryption_key.txt
```

如果 Docker 镜像还没有构建完成，也可以临时用本机 Python 生成；但项目运行本身仍推荐走 Docker。

## 6. Docker 启动

确保 Docker Desktop 已经打开。

然后运行：

```powershell
docker compose up --build -d
```

第一次构建会比较慢，因为 Docker 会下载和安装：

- Python 后端依赖
- Node 前端依赖
- 前端构建环境
- PostgreSQL
- Redis
- dirsearch
- sqlmap
- Playwright Chromium 浏览器
- 系统运行所需的 Linux 依赖

这些都装在 Docker 镜像里，不需要队友在 Windows 本机单独安装。

查看容器状态：

```powershell
docker compose ps
```

正常情况下会看到 API、Worker、Frontend、Postgres、Redis 等服务处于运行状态。

访问：

```text
http://127.0.0.1:18080
```

## 7. 创建管理员账号

首次运行时，本地数据库是空的，需要创建管理员账号。

```powershell
docker compose exec api python -m secagent.cli create-admin --username admin
```

命令会要求输入两次密码。

本地演示可以设置：

```text
用户名：admin
密码：admin
```

如果系统要求密码至少 8 位，可以设置：

```text
用户名：admin
密码：admin123
```

这是本地演示账号，不要用于正式部署。

## 8. 登录并配置模型 API Key

打开：

```text
http://127.0.0.1:18080
```

登录后进入：

```text
模型与工具
```

然后分别填写自己的：

- DeepSeek API Key
- GLM API Key

这些 Key 会加密保存在当前电脑的数据库中，不会进入 GitHub。

如果使用 OpenCode Go 作为 DeepSeek 路由，则在 `.env` 中确认类似配置：

```env
DEEPSEEK_BASE_URL=https://opencode.ai/zen/go/v1
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_STYLE=opencode-go
```

如果使用 DeepSeek 官方接口，则在 `.env` 中确认类似配置：

```env
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_API_STYLE=deepseek
```

GLM 通常类似：

```env
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
GLM_MODEL=glm-5.2
```

配置完后，回到页面里做 Provider 连通性检查。

## 9. 配置授权目标域名

如果要分析 CTF Web 题，例如：

```text
http://node4.anna.nssctf.cn:24380/
```

需要在 `.env` 中允许对应主机名：

```env
WEB_ALLOWED_HOSTS=web-demo,node4.anna.nssctf.cn
```

注意：

- 这里只写主机名，不写端口
- 多个主机用英文逗号分隔
- 修改 `.env` 后需要重启 Docker

重启命令：

```powershell
docker compose down
docker compose up --build -d
```

## 10. 创建测试任务

在页面中点击创建任务，直接用自然语言描述即可，例如：

```text
这是一道 CTF Web 题，请在授权范围内分析目标页面，尝试找到 flag，并输出完整过程报告。
目标：http://node4.anna.nssctf.cn:24380/
```

建议选择：

- 任务模式：自动判断 或 CTF / Web 被动分析
- 安全策略：标准 或 专家

如果任务需要主动探测、目录扫描、弱口令尝试、SQL 注入探测等，系统会进入人工确认流程。确认后才会调用对应工具。

## 11. 推送到 GitHub 前的检查

提交前运行：

```powershell
git status
```

确认不要出现这些内容：

```text
new file: .env
new file: secrets/deepseek_api_key.txt
new file: secrets/glm_api_key.txt
new file: secrets/provider_credential_encryption_key.txt
new file: x.db
new file: data/...
new file: .venv/...
new file: node_modules/...
new file: frontend/dist/...
```

正常提交命令：

```powershell
git add .
git status
git commit -m "chore: prepare project for github release"
git push
```

如果你看到大量：

```text
deleted: .venv/...
deleted: node_modules/...
deleted: frontend/dist/...
```

这是正常的，表示这些本地依赖和构建产物会从 GitHub 仓库中移除，不影响你本机运行。

## 12. 常见问题

### 别人 clone 后为什么没有我的任务记录？

因为任务记录在本地数据库里，不属于源码。每个人 clone 后都是自己的数据库。

### 别人 clone 后为什么需要重新填 API Key？

因为 API Key 属于个人密钥，不能提交到 GitHub。系统设计上就是每台电脑自己填写。

### 为什么不提交 `.venv` 和 `node_modules`？

这些是依赖安装产物，不是源码。别人应该通过 Docker 构建或依赖安装命令重新生成。

### 别人需要自己安装 dirsearch、sqlmap、Playwright 吗？

正常不需要。

项目推荐使用 Docker，`backend/Dockerfile` 会在镜像里安装这些工具。别人只要运行：

```powershell
docker compose up --build -d
```

Docker 会在容器里准备好工具环境。这样比每个人在 Windows 上手动安装更稳定，也更方便展示“插件化工具接入”的效果。

只有在不使用 Docker、改成本地开发模式时，才需要自己手动安装对应工具和依赖。

### 如果我已经把 API Key 推到 GitHub 过怎么办？

立刻去 DeepSeek / GLM 后台作废旧 Key，并重新生成新的 Key。只从 Git 删除文件不等于从历史记录中彻底删除。

### Docker 构建时会不会重新下载工具？

第一次构建会下载 Python、Node、Playwright、dirsearch、sqlmap 等依赖。后续如果 Docker 缓存没有失效，会复用缓存。
