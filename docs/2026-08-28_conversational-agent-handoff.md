# Conversational multi-agent workflow handoff

> 日期：2026-08-28（Asia/Shanghai）  
> 状态：Task 3 修复轮次 1 已有未提交实现，需完成全量验证、范围复审和提交。  
> 重要：不要合并或引入 `origin/hhj`；当前功能分支基于 `origin/lym`。  
> **已过时**：本文记录的状态已全部处置完毕，当前接手入口见
> `docs/2026-08-30_durable-dag-progress-report.md`。

本文档用于直接接手 `agent` 项目的传统对话式多 Agent 改造。当前工作树已冻结；没有后台 pytest 进程，也没有正在运行的子代理。

## 快速接手

```powershell
Set-Location 'F:\codex\agent\.worktrees\conversational-multi-agent'
git branch --show-current
git rev-parse HEAD
git status --short
```

预期状态：

```text
branch: feature/conversational-multi-agent
HEAD: 73f884d4dda723995efd165a3186b09174b9f0c5
modified: backend/secagent/agents/coordinator.py
modified: backend/tests/unit/test_coordinator.py
```

先复核未提交差异：

```powershell
git diff -- backend/secagent/agents/coordinator.py backend/tests/unit/test_coordinator.py
git diff --check
```

当前差异仅处理 Task 3 独立评审的两个 Important finding：

1. 保留 `budget_limits.max_context_tokens` 等后端可信预算数值，不再让通用脱敏器把它们替换成 `***REDACTED***`。
2. `CoordinatorCompletedSubtask.provider` 和 `CoordinatorTool.risk_level` 在枚举解析前拒绝 bytes、整数等非字符串输入，同时继续接受普通 JSON 字符串和枚举实例。

## 产品目标

- 传统、持久化、多轮对话框作为主要输入方式。
- DeepSeek V4 Flash 先把题目或项目拆成有依赖关系的小任务 DAG。
- GLM 负责中文语义、长文档、分类与字段提取；DeepSeek 负责分解、代码/安全、逆向/因果、证据冲突和最终综合。
- 独立子任务并行执行，每个对话默认最大并行度为 3。
- 工具请求统一经过注册范围、授权范围、RiskGate、审批与 Evidence Ledger。
- 执行中追问会 supersede 尚未执行的旧工作并重新规划。
- 模型失败只做一次同 provider 传输重试，之后暂停让用户选择；禁止自动跨 provider fallback。
- 最终回答固定由 DeepSeek V4 Flash 流式综合，并标注证据与不确定性。
- 前端目标：左侧对话列表、中间聊天区、右侧实时 DAG；`/tasks/new` 重定向到 `/chat/new`。

权威文档：

- `docs/superpowers/specs/2026-08-26-conversational-multi-agent-workflow-design.md`
- `docs/superpowers/plans/2026-08-28-decomposition-capability-routing.md`

## 已完成的检查点

| 检查点 | 提交范围 | 验证/评审状态 |
|---|---|---|
| 清理生成物和测试数据 | `3a2d9ff3` | 完成 |
| 导入并批准会话式工作流规格 | `41b9deb0..1e7fcccc` | 完成 |
| 稳定前后端基线 | `28da8fea..168c7656`、`960d68e9` | 前端 41/41 + build；后端 304/304；评审通过 |
| 会话 schema 与严格 DTO | `bad6dc11..2560a615` | 后端 321；独立评审通过 |
| 会话 repository | `81a96b32..53aecc94` | 后端 345；并发/事务复审通过 |
| 会话 service 与附件存储 | `69cdae2e..2fc0fa0c` | 非 PG 413 + 1 skip；PG barrier/focused 通过；复审通过 |
| 会话 HTTP/multipart/SSE | `980270ad..966fdec7` | focused 121；后端 522 + 1 skip；复审通过 |
| 严格分解契约（Task 1） | `320cb922..d4cc3462` | 31 focused；修复复审通过 |
| 确定性能力矩阵与分配策略（Task 2） | `4e2536fc..a54a761b` | 控制器验证 47；修复复审通过 |
| DeepSeek Flash Coordinator 初始实现（Task 3） | `73f884d4` | 151 focused；603 passed + 1 skipped；独立评审提出 2 个 Important，修复尚未提交 |

关键提交序列：

```text
73f884d4 feat: add DeepSeek decomposition coordinator
a54a761b fix: harden assignment policy controls
4e2536fc feat: add deterministic model assignment policy
d4cc3462 fix: enforce positive decomposition plan versions
320cb922 feat: add strict decomposition contract
f3318f90 docs: resolve decomposition plan preflight
c1195441 docs: plan decomposition and capability routing
966fdec7 fix: harden conversation stream admission
980270ad feat: expose conversation HTTP and SSE boundary
2fc0fa0c fix: harden conversation commit and storage boundaries
69cdae2e feat: add conversation service and attachment storage
53aecc94 fix: harden conversation repository transactions
81a96b32 feat: add conversation persistence repository
2560a615 fix: harden conversation input validation
```

## 当前未提交修复

| 文件 | 改动 | 对应 finding |
|---|---|---|
| `backend/secagent/agents/coordinator.py` | 只对不可信 context 做脱敏；重新放入已验证的数值 budget；为 provider/risk enum 增加字符串前置校验 | Task 3 I1、I2 |
| `backend/tests/unit/test_coordinator.py` | 断言两处 budget 完整保留；覆盖 bytes/整数拒绝；覆盖 JSON 字符串和枚举实例 | Task 3 I1、I2 |

独立评审与报告：

- `.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-review-findings.md`
- `.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-report.md`

当前验证事实：

| 证据 | 结果 | 可信边界 |
|---|---|---|
| 原实现代理的 I1/I2 定向测试 | 6 passed | 代理报告；未形成提交 |
| 原实现代理的扩大聚焦集合 | 221 passed，3 warnings | 代理报告；未形成提交 |
| 交接前新鲜 `test_coordinator.py` | 28 passed，1 warning | 当前工作树直接复跑 |
| 当前 `git diff --check` | 通过 | 仅 Windows LF→CRLF 提示 |
| 完整 backend 608 项 | 未确认 | 两次长测因会话/代理中断，不能声称通过 |

初始提交 `73f884d4` 在修复前曾有两次完整结果：603 passed、1 skipped、3 warnings。它们不能替代当前未提交差异的全量验证。

## 下一位接手者的执行路径

### 1. 重新运行聚焦验证

```powershell
Set-Location 'F:\codex\agent\.worktrees\conversational-multi-agent'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = (Resolve-Path 'backend').Path

& (Resolve-Path '.venv/Scripts/python.exe') -m pytest -p no:cacheprovider `
  backend/tests/unit/test_conversation_decomposition.py `
  backend/tests/unit/test_assignment_policy.py `
  backend/tests/unit/test_coordinator.py `
  backend/tests/unit/test_model_router.py `
  backend/tests/unit/test_deepseek_provider.py `
  backend/tests/unit/test_glm_provider.py `
  backend/tests/unit/test_mock_provider.py `
  backend/tests/unit/test_config_secrets.py -q
```

重点人工断言：

- 捕获的 `ModelRequest.user` 中：
  - `payload["budget_limits"]` 等于完整 `TurnBudgetSnapshot`。
  - `payload["context"]["budget"]` 等于同一份完整预算。
  - `max_context_tokens` 是整数，不是 `***REDACTED***`。
  - token、API key、密码和未授权工具名仍不出现。
- bytes/整数形式的 provider 和 risk level 必须产生 `ValidationError`。
- JSON 字符串 `"glm"`、`"deepseek"`、`"low"` 仍可正常解析。

### 2. 运行完整后端验证

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& (Resolve-Path '.venv/Scripts/python.exe') -m pytest -p no:cacheprovider backend/tests
git diff --check
```

当前收集数预计为 608；环境相关的 PostgreSQL 测试可能有 1 项预期 skip。不要把 Starlette/httpx、Python 3.12 sqlite 弃用警告误当成本轮新增失败，但需记录实际 warning 数量。

### 3. 更新报告并提交修复

将实际新鲜结果追加到：

```text
.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-report.md
```

然后只提交两个代码/测试文件：

```powershell
git add -- `
  backend/secagent/agents/coordinator.py `
  backend/tests/unit/test_coordinator.py
git commit -m "fix: preserve coordinator budget controls"
```

不要把 `.superpowers/sdd/` 报告、本文档或其他不相关文件混入这个修复提交。

### 4. 生成范围复审包

提交完成后读取新哈希并生成复审包：

```powershell
$fixCommit = git rev-parse HEAD
& 'C:\Program Files\Git\bin\bash.exe' `
  'C:/Users/32387/.codex/plugins/cache/openai-curated-remote/superpowers/6.3.0/skills/subagent-driven-development/scripts/review-package' `
  'docs/superpowers/plans/2026-08-28-decomposition-capability-routing.md' `
  '73f884d4' `
  $fixCommit
```

范围复审只检查：

- I1：预算值完整保留，且不削弱不可信文本脱敏。
- I2：非字符串枚举输入被拒绝，普通 JSON 字符串/枚举实例仍可用。
- 修复差异是否新增 Critical/Important/Minor。

复审报告建议写入：

```text
.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-fix-round-1-rereview.md
```

只有复审结论为 APPROVE 后，才把 Task 3 和 Decomposition and capability routing 阶段标记为 complete。

### 5. 封账后进入下一阶段

先更新：

- `.superpowers/sdd/2026-08-28-decomposition-capability-routing/progress.md`
- `.superpowers/sdd/progress.md`

下一阶段不是前端，而是 Durable DAG execution and recovery，范围至少包括：

- 分解结果、分配决策和 DAG 节点的持久化。
- 依赖满足后的调度、默认并发 3、重启恢复和幂等执行。
- 单 provider 一次传输重试，以及用户可选择的失败处置动作。
- 执行中追问触发 supersede 和 replan。
- 统一工具/RiskGate/审批/Evidence Ledger。
- 仅 DeepSeek V4 Flash 的最终综合与证据标注。

该阶段应先写独立实施计划并做接口/冲突预检，不要直接把调度逻辑塞进 `CoordinatorAgent`。

## 不变量与常见陷阱

- `DECOMPOSE` 和 `SYNTHESIZE` 在 live/auto 模式固定属于 DeepSeek，且模型名必须精确为 `deepseek-v4-flash`。
- 固定阶段缺少 DeepSeek 或模型名不符时必须安全失败；禁止退回 GLM 或 Mock。
- 只有 `SUBTASK_EXECUTE` 接受逻辑 provider preference，且仅允许 `glm` 或 `deepseek`。
- Mock 仅在显式 mock 模式使用，并通过 `emulated_provider=deepseek`、`emulated_model=deepseek-v4-flash` 表明模拟语义。
- Coordinator 只能从 `ModelRouter.logical_assignment_providers()` 派生可用模型，不能相信调用方提供的集合。
- 上下文预算必须按脱敏前、UTF-8 字节数近似计算，再调用模型。
- 只对不可信 context 做通用脱敏；能力矩阵、plan version、严格数值预算属于后端可信 envelope。
- 模型返回 mapping 必须先脱敏，再用同一份 mapping 做 DTO/图验证并写回 `ModelResponse.data`。
- 当前阶段禁止持久化、发事件、排队、调用工具或捕获 `ProviderFailure`；这些属于后续 DAG 执行阶段。
- 修复轮应恢复原实现代理；本轮因 Sol 模型在恢复时被平台拒绝，ledger 已记录改用 Terra 只做验证/提交的裁决。
- 工作树可能显示 LF→CRLF 提示；以 `git diff --check` 是否出现真实 whitespace error 为准。

## Evidence → Finding → Path

### Evidence

| ID | 观察 | 来源/复现 |
|---|---|---|
| E-001 | 当前分支为 `feature/conversational-multi-agent`，HEAD 为 `73f884d4`，仅两个 Coordinator 文件被修改 | `git branch --show-current; git rev-parse HEAD; git status --short` |
| E-002 | 当前未提交代码差异为 85 additions、16 deletions，`git diff --check` 无 whitespace error | `git diff --stat; git diff --check` |
| E-003 | 当前 Coordinator 单测新鲜通过 28 项 | `python -m pytest -p no:cacheprovider backend/tests/unit/test_coordinator.py -q` |
| E-004 | 初始 Task 3 提交在修复前通过 151 focused、603 backend + 1 skip | `.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-report.md` |
| E-005 | 独立评审验证 I1/I2，并确认其他 Task 3 不变量未发现问题 | `.superpowers/sdd/2026-08-28-decomposition-capability-routing/task-3-review-findings.md` |

### Finding

| ID | 状态 | 结论 | Evidence |
|---|---|---|---|
| F-001 | candidate | 当前两文件差异与 I1/I2 修复方向一致，并通过 Coordinator 单测，但完整 backend 和独立范围复审尚未完成，因此不能标记 Task 3 complete | E-001、E-002、E-003、E-005 |
| F-002 | validated | Task 1 和 Task 2 已完成修复轮、范围复审并获 APPROVE | 对应 Task 1/2 SDD ledger 与复审报告 |

### Continuation path

1. 从 E-001 的冻结工作树继续，不重置或丢弃两文件差异。
2. 执行聚焦测试和完整 backend 测试，形成新的验证 Evidence。
3. 提交两文件修复，使用 `73f884d4` 和新提交哈希生成范围复审包。
4. 独立复审 I1/I2；若 APPROVE，将 F-001 从 candidate 提升为 validated。
5. 更新两个 progress ledger，随后为 Durable DAG execution and recovery 编写新计划。
