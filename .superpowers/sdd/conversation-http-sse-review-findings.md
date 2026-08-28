# Conversation HTTP / multipart / SSE task review

## 1) Spec Compliance

**❌ Not compliant.** 生产边界的大部分主体实现符合 brief，但票据基础设施错误映射、inactive subject 的确定性 burn 顺序、极端 cursor 错误码，以及强制测试矩阵仍有缺口。

- ✅ 范围受控：变更只涉及 conversation REST/message、conversation ticket/SSE、错误类型、main wiring、access-log redaction、部署文档和聚焦测试；未加入 DAG/worker/provider/queue/synthesis/frontend，也未改 `/api/tasks` 路由或 task event wire。
- ✅ REST CRUD/detail 的路由、状态码、分页边界、404/403 保留、soft archive，以及 request-scoped `ConversationService` 的单 writer Session 组合均可从 diff 验证。
- ✅ JSON/multipart 基本媒体类型选择、严格 payload 结构、多文件顺序、显式 `FormData.close()` ownership、send-once、replay 200、以及专用 service error 的脱敏映射均可从 diff 验证。
- ✅ Conversation ticket 使用独立 strict claims/purpose/异常，60 秒 TTL、随机 JTI、SHA-256 digest 后 consume，并与 legacy task ticket 共用 replay store；scope 校验发生在 consume 前。
- ❌ 缺失/不可读 signing 配置时，event-ticket issuance 会先在通用 auth dependency 返回 `service_unavailable`，而非要求的 `event_stream_unavailable`。
- ❌ 带 bearer 的 inactive subject 会在 consume 前失败且不 burn；brief 要求 inactive subject 在 consume 后失败并 burn。
- ❌ 合法语法但超出 Python 整数转换限制的 cursor 会变成 generic 500，而不是 `invalid_event_cursor` 400。
- ✅ SSE generator 的 fresh Session-per-poll、limit 1000、canonical frame、yield 后推进 cursor、heartbeat、sleep、disconnect、resume headers，以及 archived read 可从 diff 验证。
- ✅ Uvicorn access logger 的 repeated/encoded-name `ticket` redaction和上游 proxy/access logger 部署要求可从 diff 验证。
- ❌ brief 明列的精确测试矩阵未完整实现，详见 Important #4。
- ⚠️ 报告声称的 `111 passed`、`512 passed, 1 skipped`、RED-to-GREEN 先后顺序及运行时 legacy 全回归结果无法仅由 diff 验证；本 reviewer 按要求未重跑全套。
- ⚠️ legacy `/api/tasks` 路由与 task ticket/event 公共实现未在该 diff 中修改，可以静态确认 wire surface 未变，但所有 legacy 运行时行为仍只能依赖未独立复跑的报告证据。

## 2) Strengths

- `backend/secagent/api/conversations.py:52-63` 精确构造一个 fresh writer Session，并让 ConversationRepository、TaskRepository、ConversationEventService、AuditService 共用该对象，同时把 session factory 单独传给 read/preflight。
- `backend/secagent/api/conversations.py:247-257` 在 `request.form()` 返回后立即进入 `try/finally`，能够覆盖成功、replay、结构/DTO 拒绝、映射异常和 unexpected service error 的上传关闭。
- `backend/secagent/auth/stream_tickets.py:137-232` 将 conversation ticket 与 legacy task ticket 的 claims、purpose 和异常面隔离；scope 在 replay-store 前校验，raw JTI 只经 SHA-256 后交给 store。
- `backend/secagent/api/conversation_events.py:38-72` 的 async generator 小而可独立测试，frame 格式、canonical JSON、poll cadence 和 Session 生命周期清晰。
- `backend/secagent/security/access_log.py:10-51` 同时覆盖 repeated ticket 参数和 percent-decoded 参数名；`docs/deployment.md:119` 明确补上外部代理/Ingress/CDN 的同等脱敏责任。
- 错误响应普遍使用固定、脱敏文案，未把 filename、idempotency key、JWT/JTI、数据库/Redis/文件系统异常细节写入响应。

## 3) Issues

### Critical

None.

### Important

1. **`backend/secagent/api/conversation_events.py:98-104` — 带 bearer 的 inactive subject 在 replay consume 前被拒绝，ticket 未 burn。**

   影响：`AuthService.authenticate_access()` 会检查 `UserRow.is_active`；因此同一 subject 已停用但携带此前签发的 bearer 时，路由在 `conversation_stream_ticket_service.consume()` 之前返回 401。聚焦复现显示首次请求为 `401 invalid_stream_ticket`，重新激活用户后同一 ticket 可 ticket-only 成功返回 200。该结果违反固定顺序以及“inactive subject response 必须 burn”的确定性契约，并使是否携带 bearer 改变同一 ticket 的 burn 语义。

   建议：把 bearer 的语法/签名/subject 绑定验证与用户 active 状态分开；consume 前只完成 brief 允许的 bearer 验证和 expected-user 绑定，consume 后统一加载 subject 并检查 active。增加“inactive + matching bearer -> 401，然后同 ticket 必须 401 replay”的回归测试。

2. **`backend/secagent/api/conversation_events.py:123-128`（关联 `backend/secagent/auth/dependencies.py:42-46`）— 应用 signing 配置缺失时，event-ticket issuance 返回错误的稳定 code。**

   影响：event-ticket route 使用通用 `current_user`。当 `settings.jwt_key()` 缺失/不可读/ malformed 时，鉴权 dependency 在 route body 和 `ConversationStreamTicketService.issue()` 之前抛出通用 HTTP 503；全局映射 code 为 `service_unavailable`，不是 brief 对 signing infrastructure failure 要求的 `event_stream_unavailable`。现有测试只把已配置应用中的 conversation ticket service 替换为 `signing_key=None`，没有覆盖真实 app 配置缺失的 issuance 路径。

   建议：为 event-ticket issuance 提供窄范围鉴权 dependency，把 `AuthenticationConfigurationError` 映射为 `EventStreamUnavailable`，不要改变其他 REST 或 legacy task route 的 wire；补 absent/unreadable/malformed app signing 配置的 route-level 精确 code 测试。

3. **`backend/secagent/api/conversation_events.py:74-83` — 超长十进制 cursor 可触发 unhandled `ValueError` 并返回 generic 500。**

   影响：正则接受任意长度的 ASCII 数字，但 CPython 对十进制字符串转整数有默认 digit limit；例如超过该限制的 `after` 或 `Last-Event-ID` 会在 `int(raw, 10)` 抛 `ValueError`，绕过 `invalid_event_cursor` 400。该输入在 ticket consume 前即可由未认证请求触发。较短但超出数据库 BIGINT 范围的值还可能在 consume 后进入查询并导致流失败。

   建议：在 consume 前显式捕获整数转换失败，并对底层 cursor 可表示范围采取确定策略（400 或安全地视为无后续事件），确保所有不安全/不可表示 cursor 都不会进入 DB 查询；为 query/header 两条路径加入超长及 BIGINT-overflow 用例，并验证 ticket 不 burn。

4. **`backend/tests/integration/test_conversation_api.py:139-170,186-214,606-624`；`backend/tests/integration/test_conversation_events_api.py:318-351,528-564` — brief 强制的精确测试矩阵仍不完整。**

   影响：当前测试只断言 JSON send 后 queue 为空，没有 instrument/assert 任何新 route 不调用 provider/model；多附件用例验证文件落盘但未执行 exact replay，replay 用例只有零附件；Authorization 参数表没有“语法有效但 token 无效”的 Bearer；replay-store 只模拟一种 generic exception，没有分别覆盖 before-apply 与 outcome-unknown；真实缺失 signing 配置的 issuance 也未覆盖。上述缺口已经让 Important #1/#2/#3 逃过报告所称的矩阵，并且不满足 brief 的 minimum cases 4、10、13、14。

   建议：按 brief 原文补齐 route-level spies 和 ticket-reuse/error-code matrix：多附件 first/replay 的 DTO、DB 行及文件一致性；所有新 route 的 queue/provider/model 零调用；invalid Bearer no-burn；两类 replay-store exception 均 503、单次 consume、indeterminate burn；真实 app signing 配置异常；以及本 review 指出的 bearer-inactive 与 oversized cursor 回归。

### Minor

None.

## 4) Task quality

**Needs fixes.** 计数：Critical 0，Important 4，Minor 0。票据 burn 与错误码属于 brief 明确规定的安全/协议门禁，不能在当前状态批准。
