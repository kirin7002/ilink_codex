# Docker 部署到阿里云

在服务器直接从 GitHub 拉取代码并本地构建镜像。目标为 `47.116.102.49` 上的 kirin 用户；需要该用户能够使用 Docker Engine 和 Docker Compose v2（支持 `up --wait`），以及访问 GitHub、Docker Hub、Debian、PyPI、npm、微信 iLink 和 Codex 服务的网络。脚本不自动安装 Docker 或修改宿主机防火墙。

当前已验证：镜像构建、容器内 30 项自动测试、真实 Codex 沙箱限制、Compose worker 健康检查及重启数据保留。目标阿里云服务器尚未执行这些命令；微信 bot 登录、真实模型任务和多人连接仍待验收。

## 首次部署

登录服务器：

```bash
ssh kirin@47.116.102.49
```

以下命令在服务器上运行：

```bash
git clone --branch main https://github.com/kirin7002/ilink_codex.git ~/wechat-codex-docker
cd ~/wechat-codex-docker
bash deploy/docker.sh build
bash deploy/docker.sh sandbox-check
bash deploy/docker.sh import-auth ~/.codex/auth.json
bash deploy/docker.sh login
bash deploy/docker.sh up
```

仓库为私有时，服务器需已有 GitHub 仓库读取权限；使用服务器已有的 HTTPS 凭据或 SSH 部署密钥，不把令牌写入 URL 或命令历史。若目标目录已经存在，不覆盖它；已有该仓库的 checkout 可在确认本地修改后使用 `git pull --ff-only origin main` 更新。

无法从 GitHub 拉取时，也可将源代码部署包上传后解压到一个新的目录，再执行相同的 build/login/up 步骤。

`import-auth` 复用服务器原有 Codex 登录文件，将其私密地写入独立 Docker volume，过程中不输出凭据。不读取或复制宿主的其他 Codex 配置，不修改原文件；volume 已有登录时拒绝覆盖。后续容器独立保存刷新后的登录状态，不反复导入旧文件。如果服务器没有可用登录，改用：

```bash
bash deploy/docker.sh codex-login
```

按 CLI 提示在浏览器完成设备授权。设备登录需账号支持；登录状态有效性仍以真实模型请求为准。

`login` 会在终端显示微信二维码，用本人微信扫描。账号凭据与扫码用户白名单保存在 state volume。不要把二维码转发给陌生人。

`up` 会先检查实际 Codex 沙箱、Codex 登录状态和 iLink 凭据/owner 白名单，再启动两个服务并等待健康检查。预检查失败时不启动；如果启动后的健康检查超时，命令失败但容器可能仍存在，可用 `status` 和 `logs` 诊断。

两项服务健康后，向新 bot 发送“在当前目录创建 hello.txt，内容为 hello，并告诉我结果”，确认收到任务回执和最终结果；再发“读取刚才的文件”，验证续接。只有这些真实请求成功后，才算微信与模型端到端部署完成。

## 容器配置

- `bridge` 与 `worker` 使用相同镜像、不同进程；不映射入站端口，不挂载 Docker socket，不使用宿主网络。
- 非 root UID 1000、只读根文件系统、移除全部 capabilities、禁止新增权限。
- `state` volume 保存任务数据库、bot 凭据与白名单；`work` 保存工作文件；`codex-home` 保存模型登录与会话。容器重建不会清空它们。
- worker 默认 2 GiB、2 CPU、256 进程；bridge 默认 256 MiB、0.5 CPU、64 进程。按服务器内存调整 `compose.yaml`，避免挤占 Muse 桥资源。
- 修改任务超时或并发：编辑 `deploy/docker-config.toml`，重新 `build` 和 `up`。
- 日志轮转为每服务 3 个文件，每个最多 10 MiB。无额外看门狗；Docker restart policy 处理进程退出。unhealthy 是告警状态，不会单独触发 Docker 自动重启。
- bridge 健康检查要求最近 120 秒内有成功拉取且持有服务锁；worker 要求调度循环心跳和服务锁。健康不等于模型额度或端到端消息已验证。

## Codex 沙箱兼容性

固定 CLI 0.163.0。该版本默认使用 bubblewrap；Docker 默认 seccomp 通常禁止它创建所需命名空间。worker 因此使用 `codex-seccomp.json`，基于 Moby 官方默认 profile：

`https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json`

仅追加 bubblewrap 所需的 namespace/mount 系统调用：`unshare`、`clone`、`setns`、`mount`、`umount2`、`pivot_root`、`chroot`、`sethostname`、`setdomainname`。保留默认拒绝策略及其他规则，未授予宿主 capabilities。上游许可证见 `MOBY-LICENSE`。bridge 继续使用 Docker 默认 seccomp。

`sandbox-check` 在真正可写的 state volume 建立探针，然后验证：工作区内可写、工作区外写入被拒绝、网络 socket 创建被拒绝。无需模型凭据，也不连接外网。

目标宿主内核、AppArmor 或 user namespace 策略仍可能阻止 bubblewrap。如果检查失败，请保留错误输出并诊断宿主支持；不要改用 privileged、关闭整个 seccomp、赋予 SYS_ADMIN 或跳过沙箱。CLI 的旧 Landlock fallback 在此版本不能承载该文件系统限制，因此不采用它。升级 CLI 后必须重新验收沙箱。

## 日常管理

```bash
bash deploy/docker.sh status
bash deploy/docker.sh logs
bash deploy/docker.sh stop
docker compose run --rm bridge allow list
docker compose run --rm bridge allow add --user '已核实的微信用户ID'
docker compose run --rm bridge allow remove --user '微信用户ID'
```

重新登录微信时先 `docker compose stop bridge`，再 `docker compose run --rm bridge login --replace`，最后 `bash deploy/docker.sh up`。不在微信中远程加白。

`docker compose down` 保留数据卷；不要使用 `down -v`，它会删除数据库、登录和工作文件。备份前先停止服务，再备份三个 volume；恢复前确保 UID 1000 可访问。版本更新时替换源代码并重新 build/up，保留 volume。

## 有代理的构建环境

普通服务器不需要设置 CA 变量。需要公司或受管代理证书时，通过 BuildKit 临时挂载可信 CA bundle：

```bash
ILINK_BUILD_CA=/path/to/combined-ca-bundle.pem bash deploy/docker.sh build
```

Docker daemon 自己的镜像拉取代理和信任由服务器配置。此参数只影响构建步骤，不将本次代理证书写进镜像。运行时若还需代理和 CA，按目标环境配置只读证书挂载及应用信任，不关闭 TLS 验证。
