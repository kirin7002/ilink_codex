# Codex 微信专线设计

更新：2026-10-21。范围：个人项目，先实现本人可用的文字任务闭环，框架预留多人；之后实测第二个微信用户如何连接。

## 目标与边界

- 新的 iLink bot、独立 Python 服务和目录，与现有 Muse 桥分开部署。
- 微信文字 → Codex CLI 执行代码任务 → 微信文字回复；同一用户连续对话。
- 第一版不处理图片、语音、文件或群聊，不提供微信远程加白、提权或通用人工审批。
- 白名单成员均视为可信使用者。工作目录按用户分开，不承诺不同用户文件不可读。
- 先验证本人使用，再验证多人连接。官方多账号支持不等同于一个 bot 可被五个人直接加好友；具体接入方式待实测。

## 架构

```text
微信 ↔ iLink ↔ Python bridge ↔ SQLite ↔ Python worker ↔ Codex CLI
                白名单/收发      任务/回复      每用户串行
```

收发与执行是两个 systemd 服务。bridge 长轮询收取消息，执行本地白名单校验，保存任务并排队发送回执；worker 原子领取任务、运行 Codex，保存结果；bridge 独立发送结果。模型执行期间持续收消息，支持查询和取消。

使用 Python 3.12+，第三方 `weixin-ilink==0.3.5` 的底层 API；协议适配集中在 `transport.py`。不依赖 SDK 的默认 ACL、内存 context 缓存或游标管理。官方参考：[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)。

## 目录与持久化

目标部署目录 `/home/kirin/wechat-codex/`：

| 路径 | 内容 |
| --- | --- |
| `src/ilink_codex/` | bridge、worker、Codex 和 iLink 适配层 |
| `.venv/` | Python 依赖 |
| `config.toml` | 本地非秘密配置 |
| `state/creds.json` | bot 凭据，0600 |
| `state/allowlist.json` | bot 账号 → 允许使用的微信用户 ID，0600 |
| `state/tasks.db` | SQLite WAL；任务、会话、回复、消息去重、拉取游标 |
| `state/*.lock` | 同一 state 目录内 bridge/worker/admin 单实例锁 |
| `work/<hash>/` | 按 bot 账号 + 微信用户 ID 哈希得到的工作目录 |
| `deploy/` | systemd 模板 |

state 与 work 必须互不包含。运行时设置 umask 0077。完整结果保存在数据库，微信长回复有上限。数据库含消息内容和回复上下文，按私密数据管理；第一版不自动删除历史，后续按实际用量增加保留策略。备份 SQLite 应使用 SQLite backup API，或停止两个服务再备份数据库及 WAL，不单独复制正在运行的主数据库。

## 陌生人访问控制

1. 本地执行登录，由本人扫描二维码；登录响应的 `user_id` 成为初始授权用户。缺少该 ID 就拒绝初始化，不把第一条消息的发送者当 owner。
2. 每条消息在入队前，用 bot 账号 + 服务端 `from_user_id` 校验白名单；不信昵称、正文身份或模型判断。
3. 白名单为空、缺失或损坏，一律拒绝。拒绝消息不入队、不回复、不调用模型。
4. 仅服务器本地 CLI 可增加或删除成员。未来多人测试时，在其他可信渠道核实用户 ID 后手动加白；识别短语不是自动授权口令。
5. worker 执行前和执行期间复查白名单，撤销授权后停止当前任务；bridge 发送前也复查。
6. 凭据和白名单放在工作区外，禁止将内容放入提示词或日志。当前共享系统用户不提供读取层面的强隔离，不能把 0600 或 workspace-write 描述为跨用户保密机制。

## 任务状态和消息可靠性

- 使用 `(bot账号, message_id)` 去重，只接受有 ID、回复上下文的完整用户消息。
- 同一拉取批次的任务、回执及 `get_updates_buf` 在一个事务中提交；失败时整体回滚，不越过未保存消息。
- 状态：`queued → running → succeeded / failed / timeout / cancelled / interrupted / blocked`。
- 每用户串行；全局并发可配置 1–5，默认 1。每用户最多 10 个未完成任务，默认消息上限 16000 字符。
- worker 重启将遗留 running 标为 interrupted 并通知用户，不自动重跑。失败可能发生在文件修改之后，不能承诺事务回滚。
- 回复与执行分开重试：每段回复有稳定 client_id，最多尝试 5 次，指数退避。服务端已接收但本地未确认时仍可能重复显示，不能承诺端到端 exactly-once。
- 按用户保持待发送回复顺序；一个用户发送失败不阻塞其他用户。达到上限保存为 failed；用户可用 `/result` 获取最近结果。
- 回复时使用该账号、该用户最新持久化的 `context_token`。它不是 Codex session ID。bridge 重启不丢失上下文，但服务端有效期和长任务回复是否接受仍需实测。
- `ret` 和 `errcode` 均检查；SDK 的 HTTP 成功不等于发送成功。拉取遇到 -14 按官方实现暂停一小时，其余失败退避；异常日志不输出原始响应或凭据。

## Codex 执行与会话

- 目标 CLI 版本 0.163.0；当前开发环境已核对 `exec` 和 `exec resume` 参数。
- 使用参数数组、stdin 传入提示词，禁止 shell 字符串拼接；JSONL 读取会话 ID、最终 agent_message 和 turn.completed。只有正常退出且完成事件、非空回复齐全才报成功。
- 显式使用 workspace-write、approval_policy=never、工具沙箱无网络，关闭额外 writable_roots 和共享 tmp 写权限，关闭 web_search，忽略用户 config/rules。不会自动提权，不采用 `--approve-for-me`。
- CLI 主进程连接模型仍需要联网；沙箱工具无网络不代表整个 CLI 离线。MCP 或其他扩展不作为第一版能力。
- 同一 `(bot账号, 微信用户ID)` 保存一个 session ID；后续用 `exec resume`。保持该用户工作目录不变。
- 不根据通用错误自动重放任务或清空会话。用户可以 `/new` 开新会话；工作文件保留。没有可靠的上下文长度阈值前，不实现自动“会话过长重置”。
- 默认超时 600 秒；取消、超时、正常结束都清理普通子进程组，不支持常驻后台任务。systemd 用 KillMode=control-group 清理服务所有后代。
- CLI stdout 累计上限 8 MiB；超限终止任务，避免无限输出耗尽内存。错误和退出不会把中间回复当作完成结果。
- 微信消息中的“确认”不是权限授权协议。第一版不具备对工作区内每次删除都要求人工审批的能力；重要文件应使用 Git 或备份。

## 微信指令

| 指令 | 行为 |
| --- | --- |
| 普通文字 | 排队执行，立即发送任务编号回执 |
| `/status` | 查询本人运行及排队任务 |
| `/cancel` | 取消本人排队任务并请求终止当前任务，不回滚文件 |
| `/new` | 空闲时清除 session 映射，下条消息新建会话 |
| `/result` | 查询本人最近有结果的任务 |
| `/help` | 显示帮助 |

默认每段正文最多 1000 字符；总正文超过 12000 字符时截断并提示完整结果已保存在服务器。这里是保守的应用上限，不声称是微信协议的精确长度限制。

## 运维与资源

Docker 为当前首选部署方式，见 [Docker 部署说明](deploy/DOCKER.md)。Compose 运行独立 bridge/worker，持久化 state/work/codex-home；非 root、只读根文件系统、不开放入站端口。worker 使用基于 Moby 默认配置的有限扩展 seccomp profile，使非特权 bubblewrap 可创建命名空间。启动前验证真实 CLI 的工作区写入、越界写入拒绝和无网络；不使用已不适用的旧 Landlock fallback。

容器健康检查同时验证服务锁和最近心跳：bridge 心跳只在成功拉取后更新，worker 每十秒更新。容器存活或健康不代表模型/微信真实任务已验证。Docker 模式使用 restart policy，无需同时启用下面的宿主 systemd 模板。

- 只用 systemd 的 Restart=on-failure，不叠加独立看门狗。
- systemd 模板使用无 sudo 的 kirin 用户、NoNewPrivileges、ProtectSystem=strict 和限定写入路径。
- worker 默认 MemoryMax=2G、CPUQuota=200%、TasksMax=256；须根据服务器和 Muse 桥负载调整。五并发性能不能仅凭“等待 API”推断。
- systemd 的服务级限制保护宿主资源；并非每用户独立容器，也不能防止授权用户读取该系统身份本来可读的文件。
- 凭据过期、CLI 登录失败、服务端限制或额度不足需要重新登录/处理，不通过盲目自动重启或任务重放规避。

## 验收与当前状态

第一阶段：代码、自动测试和部署模板。测试覆盖默认拒绝陌生人、消息去重、批次游标事务、同用户串行/不同用户调度、回复重试、会话续接映射、取消、重启恢复、真实子进程的超时和后代清理。

第二阶段：在目标服务器配置 bot 和 Codex 登录，实测本人收发、两次连续对话、长任务回复、bridge 重启后回复、撤销白名单。

第三阶段：实测第二个微信用户的接入方式及回包路由，再提高全局并发验证资源使用。代码内的模拟多人调度测试不等同于微信平台多人连接已经验证。

原方案记录的服务器版本、网络和登录情况仅作为用户提供的历史信息；本轮未连接阿里云服务器，不据此宣称真实服务已上线。
