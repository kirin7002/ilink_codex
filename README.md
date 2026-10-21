# ilink-codex

个人微信 iLink → Codex CLI 专线。Python 收发桥和 worker 通过 SQLite 通信，默认只接受本地白名单用户。设计见 [DESIGN.md](DESIGN.md)。

服务器推荐使用 [Docker Compose 部署说明](deploy/DOCKER.md)，已提供镜像、持久化卷、健康检查和部署脚本。以下 venv/systemd 步骤仍可用于本地开发或直接部署。

## 本地开发

需要 Linux、Python 3.12+。Codex CLI 推荐已核对参数的 0.163.0；测试不需要微信、模型凭据或外部服务。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m unittest discover -s tests -v
cp -n config.example.toml config.toml
.venv/bin/ilink-codex --config config.toml doctor
```

`doctor` 只检查 CLI 版本、命令参数、登录状态和 iLink 凭据文件是否存在；它不证明模型请求或微信收发可用。

## 目标服务器首次启动

将项目放到 `/home/kirin/wechat-codex`，用 kirin 用户执行上面的安装步骤。根据实际 CLI 位置设置 `config.toml` 的 `codex_bin`，例如 `/home/kirin/.local/bin/codex`。

```bash
codex login status
# 仅当尚未登录或登录失效时执行 codex login。
.venv/bin/ilink-codex --config config.toml login
```

用本人的微信扫描终端二维码。登录成功后保存 `state/creds.json`，并将扫码用户加入 `state/allowlist.json`。没有用户 ID 时拒绝初始化；已有凭据需要显式 `login --replace` 才能重新登录。重新登录前先停止 bridge，登录后重启 bridge 使新凭据生效。

在两个终端分别运行：

```bash
.venv/bin/ilink-codex --config config.toml bridge
.venv/bin/ilink-codex --config config.toml worker
```

向 bot 发送文字即可。支持 `/help`、`/status`、`/cancel`、`/new`、`/result`。同一账号用户使用独立工作目录和会话映射；默认全局并发为 1，可将 `max_concurrency` 调至 2–5 做多人验证。

**这里只承诺工作区分开，不承诺用户间文件不可读。**部署面向本人和可信使用者，不是多租户服务。Codex 工作区可写，删除或取消后的文件修改不会自动回滚。

## 白名单

所有授权只能在服务器本地修改：

```bash
.venv/bin/ilink-codex --config config.toml allow list
.venv/bin/ilink-codex --config config.toml allow add --user '已核实的微信用户ID'
.venv/bin/ilink-codex --config config.toml allow remove --user '微信用户ID'
```

可用 `--account` 指定 bot 账号，默认使用登录凭据中的账号。白名单每次检查都会重新读取；为空、缺失、损坏均拒绝任务。用户 ID 应从可信的登录/接入记录中取得并核实，不根据昵称、收到的自我介绍或“第一个发消息的人”授权。实际多人连接方式仍待微信侧实测，代码不自动注册陌生人。

## systemd

先验证前台完整收发，再安装 `deploy/` 中两个模板。模板使用 kirin 用户和固定路径，若路径不同须修改 WorkingDirectory、ExecStart、ReadWritePaths 和 PATH。确认 `state/`、`work/`、`/home/kirin/.codex` 已存在。由服务器管理员将模板放入 `/etc/systemd/system/` 后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-codex-bridge wechat-codex-worker
sudo journalctl -u wechat-codex-bridge -u wechat-codex-worker -f
```

服务本身不使用 sudo。worker 服务的资源上限是全局上限；请按目标机器内存调整。正式运行使用 systemd，确保整个进程组/cgroup 在停止或崩溃后清理；不要同时手工启动第二个 worker。锁会拒绝重复实例。

## 网络与故障

- bridge 需要 HTTPS 访问 iLink API 和登录响应指定的地址；只有文字时不下载媒体。保持 TLS 验证。
- Codex CLI 主进程需要访问其模型和认证服务；生成命令的沙箱显式关闭网络，因此安装依赖、远端 Git 操作可能无法在任务中进行。
- Python SDK 为第三方 0.3.5，腾讯协议可能变化。首次登录与真实收发仍需目标环境验收，协议差异集中修复适配层。
- HTTP 200 也检查业务错误码。失败回复退避重试，最多五次；用户再次发送 `/result` 可使用最新上下文获取结果。
- 超时、失败、中断不会自动重跑代码任务。服务重启将遗留 running 标为 interrupted，排队任务继续处理。
- 日志只记录异常类别和任务/回复编号，不打印原始 SDK 异常。诊断真实 CLI 问题时，在服务器上以相同用户和工作目录手动验证登录及请求，不把凭据贴到聊天或日志。
- SQLite 保存完整任务结果和敏感回复上下文，应限制访问、定期备份和观察磁盘使用；当前不自动清理历史。

## 实测顺序

1. 本人发送任务，收到回执和最终结果。
2. 连续发送两条消息，确认会话记忆和工作文件保留。
3. 运行中 `/status`、`/cancel`，确认后代进程停止；验证 `/new`。
4. 重启 bridge 后获取回复；重启 worker 后中断任务不自动重跑。
5. 临时移除本人白名单，确认不能触发任务，然后在服务器本地恢复。
6. 第二个微信用户接入后，核实 ID 并手工加白，验证任务、会话和回复不串用户。

自动测试使用模拟传输和假 CLI 子进程，不代表真实 iLink 连接、模型调用、沙箱边界或 systemd 部署已经通过验收。
