# Conversation HTTP/SSE scoped pre-flight re-review

结论：**PASS**。

审查基线仍为 HEAD `2fc0fa0cbb244d3338715e0f91c1898fac60738f`。本轮只复核上一版 pre-flight 的 C1、C2、I1–I4；未扩大到新设计审查，未改代码或 brief。

## 原开放项复核

### C1 — ADDRESSED

query-string ticket 与日志隐私现已形成可执行的分层契约：

- 应用代码不得持久化、审计或主动记录 raw JWT/JTI；
- production surface 明确加入 Uvicorn access-log redactor；
- redactor 必须覆盖 percent-decoded 名称恰为 `ticket` 的普通、重复和 encoded-name 参数；
- 自动化测试必须证明已签发 JWT 不在 log record 中且替换标记存在；
- 外部 reverse proxy 明确属于部署文档责任边界，必须禁用该路由日志或做同等脱敏。

因此应用内绝对要求不再与标准 Uvicorn full-query access log 冲突，外部不可控边界也被准确限定。

### C2 — ADDRESSED

UploadFile 生命周期现已按所有权分成两个可测路径：

- `request.form()` 成功返回后，应用立即进入 `try/finally` 并 `await form.close()`，覆盖 accepted、unknown/rejected-field uploads 以及 success/replay/validation/service/unexpected failure；
- parser 在返回 `FormData` 前失败时，明确由 Starlette parser-owned cleanup 负责，应用只做 sanitized 422 映射；
- 测试矩阵第 12 项要求分别证明这两个路径。

这消除了“应用必须异步 close 一个从未交给应用的 UploadFile”的不可实现要求。

### I1 — ADDRESSED

ticket 错误分类和 HTTP 映射已经闭合：

- missing/unreadable signing config、JWT signing failure、replay-store exception 是 infrastructure unavailable，issuance 与 consume 均映射 503 `event_stream_unavailable`；
- signature/expiry/required claims/purpose/user/resource mismatch 以及 replay-store `False` 是 invalid ticket，映射 401 `invalid_stream_ticket`；
- replay-store exception 的 burn state 明确为 indeterminate，禁止自动重试旧 JWT，并要求获取新 ticket；
- raw exception 不得泄漏；
- signing secret 不可用不再允许 `create_app()` 崩溃，同时明确保持 legacy task route contract；
- 测试矩阵第 13 项覆盖配置、encode/decode、store false/exception 和精确 401/503。

现有 legacy service 可以保持签名/claims/method surface；conversation service 可用独立异常类型实现，不存在必须弱化 legacy ticket 的冲突。

### I2 — ADDRESSED

optional bearer 与 ticket 消耗顺序已经逐项确定：

- header 缺失才进入 ticket-only；header 存在时必须恰好一次、恰好一个合法 Bearer credential；
- wrong scheme、empty/malformed/duplicate bearer、`AuthenticationError` 为不消耗 ticket 的 401 `invalid_stream_ticket`；
- `AuthenticationConfigurationError` 为不消耗 ticket 的 503 `event_stream_unavailable`；
- 固定顺序明确为 cursor → optional bearer → cryptographic/claim/URL/user scope → atomic consume → active user → conversation authorization/read；
- inactive、post-consume 404、post-consume 403 明确 burn；cursor/bearer/claim/scope/pre-store infrastructure failure 明确不 burn；store exception 明确 indeterminate。

测试矩阵第 14 项要求完整复用矩阵，因此实现与断言不再需要猜测。

### I3 — ADDRESSED

manual JSON/multipart validation 现已给出唯一分类：

- base media type 大小写不敏感但必须精确等于两个允许值，参数允许，prefix/suffix lookalike 不允许；
- JSON decode、multipart parser/structure、manual Pydantic failure 统一为 `ApiError(422, "validation_error", "Request validation failed", fields=None)`，不得序列化 `exc.errors()` 或 input；
- JSON nonempty `relative_paths` 是 transport validation error，且不得调用 service；
- structurally valid multipart 必须按序调用 service 恰好一次；count/path/ZIP/size/storage 的 `AttachmentStorageError` 唯一映射为 422 `attachment_validation_failed`；
- unsupported media 唯一映射 415 `unsupported_media_type`。

DTO/transport/storage 三层边界已经精确可测且不会泄漏值。

### I4 — ADDRESSED

测试矩阵新增第 11–14 项，完整覆盖原缺口：

- writer Session identity、与 auth/read sessions 隔离、write-entry clean state、response/exception teardown；
- returned FormData 的全路径 close 与 parser-owned failure cleanup；
- signing/JWT/replay-store 失败分类、隐私及 Uvicorn log redaction；
- optional bearer 与所有确定/不确定 burn 语义。

这些测试要求足以让上述契约先产生有意义的 RED，并提供对应 GREEN 证据。

## 新增 Critical/Important 歧义检查

未发现新的 Critical 或 Important 歧义。

- Uvicorn filter 的具体文件位置与替换标记可由实现者内部选择；外部 API/wire contract 不依赖其字面值，测试只要求一致的标记与 raw JWT 缺失，因此不是契约歧义。
- replay-store 写入异常无法判断是否已经生效这一事实被显式建模为 indeterminate，并规定旧 JWT 不重试，因此不再要求无法保证的 no-burn。
- app startup 的 unavailable-key 路径和 legacy route preservation 可以在共享同一个 replay-store、分别构造两个 ticket services 的现有 main composition 中实现，不要求改变 lifespan 或 task wire format。
- multipart parser-owned cleanup 与 application-owned async cleanup 的测试责任已被分开，不会要求 router 访问不可达的 parser 内部对象。

最终结果：**PASS — C1/C2/I1/I2/I3/I4 全部 ADDRESSED；新增 Critical 0 / Important 0。**
