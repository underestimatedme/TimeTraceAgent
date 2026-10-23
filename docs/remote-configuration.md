# 网页远程配置协议 v1

用户在网页 Agent 管理保存默认 Codex/Claude Agent 的模型、工具并行上限和整机并行上限。所有并行值限定 1–8，只允许结构化配置，不执行远程命令、不改变权限或登录凭据。

- 复用 Runner 身份、账号鉴权、inventory、接单状态、执行槽位与项目路由。
- 新增 `GET/POST /runners/:id/config`（用户令牌），`GET /runner/config` 与 `POST /runner/config/ack`（设备令牌）。inventory `config_version:1` 才允许下发。
- 新增 `tt_runner_config_operations`：按电脑递增 revision、账号级 idempotency_key、请求摘要、应用前快照、期望值与失败码。未知保存结果使用原请求重试，变更内容必须使用新 key。
- 下发设置 config_pending，现有 claim/scheduler 门禁停止新任务。Runner 等现有任务全部结束，核对本地配置与账号，校验模型，原子持久化并热更新实例和并行槽位。无需重启。
- Runner 将 revision/runner_id/desired 收据与配置一起保存，宕机、响应丢失可重放。CLI 配置写入与远程更新共用文件锁。
- 只有真实 inventory 的模型、账号及两个并行上限均与期望值一致，服务端才接受 applied。可选项目规则同步在同一事务中把同账号旧模型替换为新模型并提升路由 revision。
- 状态 queued → waiting_idle（可选）→ applied/failed。失败释放接单门禁；保留原值与固定诊断码。网页展示待处理、实际值、失败恢复和新 revision 重试。
- Codex 校验登录账号的 model/list；Claude 通过 stream-json initialize 获取 models（不发送 user 消息、不启动模型生成），两者共用目录读取与应用前校验。只传规范化模型名称、ID、解析模型、描述、账号指纹和更新时间，不传原始初始化响应。该确认代表配置生效，模型额度另行检查。

升级顺序：Valley additive migration → Runner 0.5.3rc3 → Web。旧 Runner 不上报能力时 Web 禁止保存，不假装下发成功。首次登录/配对、CLI 认证和本地目录授权仍走现有安全流程。


模型目录 v1 随既有 tools inventory 每约 5 分钟同步，成功缓存最多 240 秒，失败不保留可选项；账号或 CLI 路径变化立即失效。服务端和网页以 10 分钟为新鲜度上限，保存后电脑仍在空闲边界重新查询目录。旧 Runner 缺少目录时网页显示能力缺失而不退回手填。Claude 名称来自实际 initialize 返回，已在 CLI 2.1.302 实机验证（13 个选项）；Codex CLI 0.147.0 实机返回 3 项。
