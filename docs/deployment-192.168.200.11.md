# 192.168.200.11 Automation Center 容器部署手册

本文用于在 **CentOS Stream 9** 管理节点 `192.168.200.11` 上部署 Automation Center。该节点已经运行 `salt-master` 和 `salt-api`；被管节点 `192.168.200.12` 已运行 `salt-minion`。源码包直接使用 `wget` 下载并解压，目标机不要求安装 Git，也不会存在 `.git` 目录。

每个步骤都给出命令、预期结果和停止条件。除特别说明外，命令都在 `192.168.200.11` 的 root shell 中执行。不要把本文中的占位符、示例输出或旧环境文档中的示例密码当作生产凭据。

## 1. 固定拓扑与安全边界

```text
运维浏览器（192.168.200.0/24）
        │ HTTPS 8443
        ▼
192.168.200.11
  Automation Center 容器（host network）
       │ 127.0.0.1:8000
       ▼
  salt-api → salt-master
                │ TCP 4505/4506
                ▼
          192.168.200.12 salt-minion
```

- 应用入口：`https://192.168.200.11:8443`；`8080` 只做 HTTPS 跳转。
- `salt-api:8000` 必须仅监听回环地址，绝不向管理网开放。
- 当前测试环境关闭了 firewalld，`8080/8443` 不做主机级来源地址过滤，只能部署在已隔离的测试管理网中；如需限制来源，必须由上游网络 ACL 实现。
- 应用、SQLite、包和日志持久化在 `/var/lib/automation-center/`。
- 证书为带 `IP:192.168.200.11` SAN 的自签名证书；浏览器需人工信任。
- 当前测试环境的 SELinux 状态固定为 `Disabled`，Compose 不添加 SELinux 标签参数。
- 当前是单实例、单 Uvicorn Worker、内置 Scheduler，不能横向启动第二副本。

> 本手册对应隔离测试环境，固定使用 `admin/admin` 和
> `automation-center/automation-center`。这组弱密码不得用于生产、公网或其他共享环境。

### 1.1 部署前先认识账号、Key 和证书

本节只解释它们各自负责什么。后面的部署步骤会再说明为什么要执行对应命令。

| 名称 | 固定测试值/文件 | 作用 | 不要混淆 |
|---|---|---|---|
| Linux `root` | `.11` 的 root | 安装 Docker、修改 Salt 配置、启动容器 | 它不是 Automation Center 页面账号，也不是 salt-api 用户 |
| 页面管理员 | `admin/admin` | 登录 Automation Center Web 页面 | 只在空数据库首次启动时由 `.env` 初始化，密码随后以 Argon2id Hash 存入 SQLite |
| Salt eAuth 用户 | `automation-center/automation-center` | Automation Center 后端调用本机 salt-api 的机器账号 | 它不是 Linux 用户，也不能用于登录 Web 页面；当前部署只新增这一个专用 eAuth 用户，已有 `automation` 用户保留 |
| `/etc/salt/api-users` | `automation-center:<SHA-256>` | file eAuth 的服务端密码校验文件 | 这里只保存摘要；curl、验证脚本和应用 `.env` 必须传明文 `automation-center`，不能传摘要 |
| Minion ID 和 Minion Key | 以 `salt-key -L` 为准 | 标识并认证 `.12` salt-minion，允许 Master 管理该节点 | 它和 Salt eAuth 用户密码是两套完全不同的认证；已有 Accepted Key 时不要重新生成 |
| `AUTOMATION_CENTER_APP_SECRET` | `test-only-automation-center-app-secret` | 应用内部加密根密钥，用来派生数据库敏感字段的加密密钥 | 它不是登录密码、Salt 密码或 TLS 私钥；启动后必须保持不变，否则数据库中的 Salt credential 可能无法解密 |
| `certs/tls.key` | 部署时生成 | HTTPS 服务器私钥，由容器内 Nginx 证明服务器身份并完成 TLS 握手 | 私钥必须保密且权限为 `0600`，不能复制给浏览器 |
| `certs/tls.crt` | 部署时生成 | HTTPS 公共证书，声明服务地址是 `192.168.200.11` | 可以分发给浏览器导入信任；自签名证书不会被系统默认信任 |
| Salt Token | 登录 salt-api 后临时返回 | 后续 API 请求的短期会话凭据 | 不需要人工配置或持久保存，也不要用 `sh -x` 把它写入日志 |

整个认证关系可以简化为：浏览器用 `admin/admin` 登录 Automation Center；Automation Center 再用明文 `automation-center/automation-center` 登录本机 salt-api；salt-master 最后通过已经接受的 Minion Key 管理 `.12`。

## 2. 部署前检查

### 2.1 登录并记录系统基线

**这一步做什么：**先确认操作系统、时间、磁盘、已关闭的安全组件和现有 Salt 服务符合前提。这里只读取状态，不修改系统。

```bash
ssh root@192.168.200.11
hostnamectl
cat /etc/centos-release
timedatectl
df -h /var/lib /opt
getenforce
systemctl is-active firewalld || true
systemctl is-enabled firewalld || true
systemctl is-active salt-master salt-api
ss -lntp | grep -E ':(4505|4506|8000|8080|8443)\b' || true
salt-key -L
```

预期结果：

- 系统为 CentOS Stream 9，时间已同步。
- `/var/lib` 至少有 10 GiB 可用空间；容量测试需要额外空间。
- `getenforce` 返回 `Disabled`；firewalld 返回 `inactive`，并且不是 enabled 状态。
- `salt-master`、`salt-api` 为 `active`。
- `4505/4506` 已监听，`8000` 仅在 `127.0.0.1` 或 `::1` 监听。
- `8080/8443` 尚未被占用。
- `salt-key -L` 的 `Accepted Keys` 中能找到 `.12` 的实际 Minion ID。

停止条件：系统版本不符、时间未同步、数据盘不足、SELinux/firewalld 与本手册前提不一致、Salt 服务异常、`8000` 对外监听，或者 `8080/8443` 已被其他服务占用。先处理冲突，不要继续部署。

### 2.2 锁定实际 Minion ID

**这一步做什么：**确认 `salt-key -L` 中哪一个身份属于 `.12`，并验证 Master 已经能控制该 Minion。Minion ID 是节点身份，Accepted Key 是 Master 对该身份的信任，不涉及 `automation-center` eAuth 密码。

以下命令中的 `node-192-168-200-12` 只是示例。必须替换为 `salt-key -L` 的真实结果：

```bash
export MINION_ID='node-192-168-200-12'
salt "$MINION_ID" test.ping
salt "$MINION_ID" grains.item host fqdn_ip4
salt "$MINION_ID" cmd.run 'id; test -x /bin/bash; test -x /usr/bin/python3; command -v tar'
```

预期结果：`test.ping` 返回 `True`，grains 包含 `192.168.200.12`，Salt 执行身份是 root，三个执行依赖都存在。

停止条件：ID 与 `.12` 对不上、节点离线、命令缺失或 Salt 不以 root 执行。

### 2.3 运行源码包预检脚本

**这一步做什么：**把前面的人工检查再用只读脚本执行一次，减少漏项。首次还没有解压源码时先跳过，完成第 4 节下载和解压后执行：

```bash
cd /opt/automation-center
MINION_ID="$MINION_ID" EXPECTED_MINION_IP=192.168.200.12 sh deploy/scripts/preflight.sh
```

Docker 安装完成后再执行严格检查：

```bash
MINION_ID="$MINION_ID" EXPECTED_MINION_IP=192.168.200.12 REQUIRE_DOCKER=1 sh deploy/scripts/preflight.sh
```

预期结果：最终 `failures=0`。`WARN` 必须逐项确认；任何 `FAIL` 都是停止条件。

## 3. 安装 Docker Engine 与 Compose

**这一步做什么：**安装运行单一应用容器所需的 Docker Engine、镜像构建插件和 Compose 插件。`wget` 用于下载源码压缩包；不安装 Git。

命令依据 Docker 官方 [CentOS 安装文档](https://docs.docker.com/engine/install/centos/)。先确认没有旧版 Docker 包：

```bash
rpm -qa | grep -E '^(docker|podman-docker|containerd|runc)' || true
```

如果输出旧版 `docker-*`、`podman-docker` 或非 Docker 仓库的 `containerd/runc`，先记录清单并按官方冲突包说明处理；不要盲目删除正在承载其他业务的运行时。

在线安装：

```bash
dnf -y install dnf-plugins-core wget tar gzip openssl curl python3-pyyaml
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
docker run --rm hello-world
```

预期结果：Docker Client/Server、Compose 版本均可显示，`hello-world` 成功退出。

停止条件：软件源、镜像仓库、DNS/代理或 Docker daemon 有任何错误。镜像构建还需访问 Docker Hub、Debian apt、npm 和 PyPI。

## 4. 使用 wget 下载并固定源码包

**这一步做什么：**从公开 GitHub 仓库下载 `main` 分支压缩包，保存下载文件的 SHA-256，再解压到固定目录。目标机没有 `.git`，因此以“下载 URL + 压缩包 SHA-256 + 镜像 ID”作为本次部署版本证据。

如果你已经用 `wget` 下载了同一个文件，可以把 `SOURCE_ARCHIVE` 改成实际绝对路径并跳过 `wget`，但仍必须执行 `sha256sum` 和解压前检查。

```bash
SOURCE_URL='https://github.com/alexyao815/auto/archive/refs/heads/main.tar.gz'
SOURCE_ARCHIVE='/root/automation-center-main.tar.gz'

wget --https-only --max-redirect=20 -O "$SOURCE_ARCHIVE" "$SOURCE_URL"
sha256sum "$SOURCE_ARCHIVE" | tee /root/automation-center-source.sha256
printf '%s\n' "$SOURCE_URL" | tee /root/automation-center-source-url.txt
tar -tzf "$SOURCE_ARCHIVE" | sed -n '1,20p'

if [ -e /opt/automation-center ]; then
  echo '/opt/automation-center 已存在；先确认它是否是需要保留的旧部署，不要覆盖' >&2
  exit 1
fi
install -d -m 0755 /opt/automation-center
tar -xzf "$SOURCE_ARCHIVE" -C /opt/automation-center --strip-components=1
cd /opt/automation-center
test -f docker-compose.yml
test -f Dockerfile
test -f deploy/scripts/preflight.sh
ls -ld /opt/automation-center
```

预期结果：下载和解压均成功；`/root/automation-center-source.sha256` 第一列为 64 位 SHA-256；源码目录中存在 Compose、Dockerfile 和预检脚本。

停止条件：下载不是 HTTPS、压缩包不能列出、目标目录已有内容、关键文件缺失或 SHA-256 记录为空。不要把未知来源压缩包直接覆盖现有部署。

现在执行第 2.3 节预检；Docker 尚未严格检查时先使用默认模式。

## 5. 备份并合并 Salt 配置

### 5.1 创建可回滚备份

**这一步做什么：**在改动 Salt Master、salt-api 权限和密码摘要文件之前保存完整副本。后续配置错误时，只恢复这些文件，不碰 Automation Center 数据。

```bash
backup_dir="/root/salt-config-backup-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup_dir"
cp -a /etc/salt/master /etc/salt/master.d /etc/salt/api-users "$backup_dir" 2>/dev/null || true
printf '%s\n' "$backup_dir" | tee /root/automation-center-salt-backup-path.txt
grep -RnsE '^(netapi_enable_clients|external_auth|file_roots|rest_cherrypy):' /etc/salt/master /etc/salt/master.d 2>/dev/null || true
```

预期结果：备份目录存在，并列出当前相关配置位置。原环境通常在 `/etc/salt/master.d/api.conf` 中已有 `automation` 用户。

停止条件：无法备份，或者发现多个无法判断优先级的重复配置。此时先人工合并，不要重启 Salt。

### 5.2 配置固定测试账号和应用环境文件

**这一步做什么：**新增一个专供 Automation Center 使用的 Salt eAuth 用户，并创建应用启动所需的 `.env`。这里只新增 `automation-center` 一个 eAuth 用户；已有 `automation` 等账号原样保留。

固定测试值的含义：

- `admin/admin`：Web 页面管理员，只负责登录 Automation Center。
- `automation-center/automation-center`：应用调用 salt-api 的机器账号。`/etc/salt/api-users` 保存密码摘要，`.env` 保存应用发起登录时必须提交的明文密码。
- `test-only-automation-center-app-secret`：应用加密根密钥。它不参与 salt-api 登录，也不等于 TLS 私钥；这个值在同一数据库生命周期内不能变化。

file eAuth 的校验流程是：客户端发送明文 `automation-center` → Salt 对明文计算 SHA-256 → 与 `/etc/salt/api-users` 中的摘要比较。因此不能把 64 位摘要放入 `AUTOMATION_CENTER_SALT_API_CREDENTIAL`，否则会被再次 Hash 并导致 HTTP 401。

```bash
cd /opt/automation-center
umask 077
SALT_API_HASH="$(printf '%s' 'automation-center' | sha256sum | awk '{print $1}')"

touch /etc/salt/api-users
api_users_tmp="$(mktemp)"
awk -F: '$1 != "automation-center"' /etc/salt/api-users > "$api_users_tmp"
printf 'automation-center:%s\n' "$SALT_API_HASH" >> "$api_users_tmp"
install -o root -g salt -m 0640 "$api_users_tmp" /etc/salt/api-users
rm -f "$api_users_tmp"

{
  printf 'AUTOMATION_CENTER_INITIAL_USERNAME=admin\n'
  printf 'AUTOMATION_CENTER_INITIAL_PASSWORD=admin\n'
  printf 'AUTOMATION_CENTER_APP_SECRET=test-only-automation-center-app-secret\n'
  printf 'AUTOMATION_CENTER_SALT_API_USERNAME=automation-center\n'
  printf 'AUTOMATION_CENTER_SALT_API_CREDENTIAL=automation-center\n'
  printf 'AUTOMATION_CENTER_SALT_EAUTH=file\n'
} > .env
chmod 0600 .env
unset SALT_API_HASH
```

确认文件权限和变量名，不显示变量值：

```bash
stat -c '%a %U:%G %n' .env /etc/salt/api-users
cut -d= -f1 .env
cut -d: -f1 /etc/salt/api-users
expected_hash="$(printf '%s' 'automation-center' | sha256sum | awk '{print $1}')"
grep -Fx "automation-center:$expected_hash" /etc/salt/api-users
unset expected_hash
```

预期结果：`.env` 为 `600 root:root`；`api-users` 为 `640 root:salt`；旧
`automation` 用户仍存在，并新增唯一一行 `automation-center`。执行
`grep '^automation-center:' /etc/salt/api-users` 时，冒号后应为 64 位摘要，不能是明文 `automation-center`。

停止条件：旧用户丢失、`automation-center` 重复、摘要不是预期值、权限过宽或文件为空。使用第 5.1 节备份恢复后重新操作。此时还没有重启 salt-api，不要把本步骤只写完文件误认为账号已经完成验证。

### 5.3 合并最小权限

**这一步做什么：**告诉 salt-master 允许 `automation-center` 调用哪些功能。`api-users` 只证明密码正确；这里的 `external_auth` 才决定登录后能否查询 Key、探测节点、执行任务和查询 JID。

仓库模板 `deploy/salt/master.d/automation-center.conf` 只描述新账号。现有环境已经有 `automation` 用户、`rest_cherrypy` 和可能的 `/srv/salt`；不能直接用模板覆盖这些内容。

将 `/etc/salt/master.d/api.conf` 合并成以下目标结构。保留原 `automation` 权限和已有 Fileserver 根目录，只新增 `automation-center`、所需 client 和 `/var/lib/automation-center/packages`：

```yaml
netapi_enable_clients:
  - local
  - local_async
  - runner
  - wheel

external_auth:
  file:
    ^filename: /etc/salt/api-users
    ^filetype: text
    ^hashtype: sha256
    automation:
      - test.*
      - cmd.*
      - cp.*
    automation-center:
      - test.ping
      - grains.item
      - service.get_all
      - cmd.run
      - cmd.run_all
      - cp.get_file
      - saltutil.kill_job
      - '@runner':
          - jobs.lookup_jid
          - jobs.list_jobs
      - '@wheel':
          - key.list_all
          - key.accept
          - key.reject

file_roots:
  base:
    - /srv/salt
    - /var/lib/automation-center/packages

rest_cherrypy:
  host: 127.0.0.1
  port: 8000
  disable_ssl: true
  thread_pool: 10
```

配置中几个关键部分的作用：

- `netapi_enable_clients`：开放同步调用、异步调用、Job 查询和 Key 管理四类 salt-api 客户端。
- `external_auth.file`：指定用户摘要文件、SHA-256 算法以及 `automation-center` 的最小权限。
- `@wheel key.*`：查询、接受或拒绝 Minion Key，用于节点接入；它不是 TLS Key。
- `@runner jobs.*`：按 JID 查询异步任务，应用重启后也靠它继续监控原任务。
- `file_roots`：允许 Minion 从 `/var/lib/automation-center/packages` 读取已经验证的维护包文件。
- `rest_cherrypy.host: 127.0.0.1`：让 8000 只允许本机应用容器访问，即使 firewalld 已关闭也不会暴露到管理网。

如果实际旧账号或 Fileserver 配置与示例不同，以备份中的真实值为准。确保同一层级没有第二份 `.conf` 重复定义这些顶层键。

准备共享目录并检查 YAML：

```bash
install -d -m 0755 /var/lib/automation-center/{db,packages,logs,work,temp,temp/nginx,backups}
python3 -c 'import pathlib,yaml; yaml.safe_load(pathlib.Path("/etc/salt/master.d/api.conf").read_text())'
```

如果系统 Python 缺少 PyYAML，可用 Salt 自带 Python 执行同一段检查。任何 YAML/verify-env 错误都是停止条件。

### 5.4 重启并验证 Salt

**这一步做什么：**重载刚才的用户、权限和 Fileserver 配置，然后使用明文密码做完整最小权限验证。验证脚本会临时获取 Salt Token，但不会保存或打印 Token。

```bash
systemctl restart salt-master salt-api
systemctl --no-pager --full status salt-master salt-api
ss -lntp | grep -E ':(4505|4506|8000)\b'
MINION_ID="$MINION_ID" \
SALT_API_USERNAME=automation-center \
SALT_API_PASSWORD=automation-center \
sh deploy/salt/verify-salt-api.sh
```

预期结果：服务均为 `active`，`8000` 仍只监听回环地址，验证脚本所有项目为 `PASS`。

如需单独理解登录请求，等价的最小登录命令如下。`password` 必须是明文；不要加 `-x`，避免 Token 出现在调试日志中：

```bash
curl -sS http://127.0.0.1:8000/login \
  -H 'Accept: application/json' \
  --data-urlencode 'username=automation-center' \
  --data-urlencode 'password=automation-center' \
  --data-urlencode 'eauth=file'
```

停止条件：服务重启失败、Minion 断开、旧 `automation` 调用方失效、API 能力缺失或 `8000` 暴露。查看 `journalctl -u salt-master -u salt-api -n 200 --no-pager`，必要时按第 13.4 节回滚。

## 6. 生成自签名 TLS 证书

**这一步做什么：**给浏览器到 Nginx 的 `8443` 连接启用 HTTPS。`tls.key` 是服务器私钥，只给 Nginx 读取；`tls.crt` 是可公开的服务器证书，其中的 IP SAN 用于证明访问地址是 `192.168.200.11`。这两个文件与 Salt Minion Key、`AUTOMATION_CENTER_APP_SECRET` 都没有关系。

这里使用自签名证书，浏览器第一次访问会告警，需要人工核对 IP SAN 后信任。`-nodes` 生成无口令私钥，便于容器无人值守启动，因此必须依赖 `0600` 文件权限保护私钥。

```bash
cd /opt/automation-center
install -d -m 0755 certs
openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 825 \
  -keyout certs/tls.key \
  -out certs/tls.crt \
  -subj '/CN=192.168.200.11/O=Automation Center' \
  -addext 'subjectAltName=IP:192.168.200.11' \
  -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
  -addext 'extendedKeyUsage=serverAuth'
chmod 0600 certs/tls.key
chmod 0644 certs/tls.crt
openssl x509 -in certs/tls.crt -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in certs/tls.crt -noout -checkip 192.168.200.11
openssl pkey -in certs/tls.key -noout -check
stat -c '%a %U:%G %n' certs/tls.key certs/tls.crt
```

预期结果：`checkip` 和私钥检查成功，SAN 包含 `IP Address:192.168.200.11`；私钥为 `600 root:root`，证书为 `644 root:root`。

停止条件：私钥检查失败、私钥权限不是 `600`、证书没有 IP SAN，或者证书已过期。

## 7. 确认 SELinux 与 firewalld 已关闭

**这一步做什么：**只确认目标测试环境与已锁定前提一致，不执行关闭操作。因为 SELinux 已关闭，Compose 使用普通 bind mount；因为 firewalld 已关闭，主机不会替 `8080/8443` 做来源地址过滤。

```bash
test "$(getenforce)" = Disabled
if systemctl is-active --quiet firewalld; then
  echo 'firewalld 仍在运行，与当前测试环境前提不一致' >&2
  exit 1
fi
systemctl is-active firewalld || true
systemctl is-enabled firewalld || true
```

预期结果：`getenforce` 为 `Disabled`，firewalld 为 `inactive`，并且不是 enabled 状态。Compose 文件中没有 `:z` 或 `:Z` 挂载参数。

停止条件：SELinux 不是 `Disabled` 或 firewalld 仍在运行。此时不要照抄本文继续部署，应先确认环境策略并重新设计挂载标签或防火墙规则。

> 关闭 firewalld 后，`8080/8443` 会对所有能路由到 `.11` 的主机开放。本文只适用于隔离测试网；如果网络中存在非测试主机，应先在交换机、路由器或上游安全设备限制访问。`salt-api:8000` 仍必须靠 `127.0.0.1` 监听保持不可远程访问。

## 8. 构建并启动容器

### 8.1 配置解析和镜像构建

**这一步做什么：**先让 Compose 展开并校验配置，再把下载的源码构建成 `automation-center:1.0.0` 镜像。`.env` 中的账号和密钥只作为运行时环境变量，不应写入镜像层。

```bash
cd /opt/automation-center
docker compose config >/dev/null
docker compose build --pull
docker image inspect automation-center:1.0.0 \
  --format 'IMAGE_ID={{.Id}} CREATED={{.Created}}' | tee /root/automation-center-image.txt
```

预期结果：Compose 配置可解析，前后端依赖安装和 Vite 构建完成，镜像 ID 被记录。

停止条件：构建失败或输出中出现真实凭据。不要在故障单中粘贴 `docker compose config` 的完整环境变量部分。

### 8.2 启动并等待 Healthy

**这一步做什么：**创建应用容器，并等待容器内 Nginx、FastAPI、SQLite 迁移和 Scheduler 完成启动。Healthy 只表示应用基本可服务，真实 Salt 权限仍以第 5.4 节为准。

```bash
docker compose up -d
attempt=0
until [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' automation-center 2>/dev/null)" = healthy ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    docker compose ps
    docker compose logs --tail=200 automation-center
    exit 1
  fi
  sleep 2
done
docker compose ps
docker compose logs --tail=100 automation-center
```

预期结果：容器为 `Up ... (healthy)`；日志显示 Alembic 初始化和 Uvicorn/Nginx 正常启动，无 traceback。

停止条件：60 秒仍不 Healthy、迁移失败、证书不可读、端口冲突或持续重启。

## 9. 部署后自动验证

**这一步做什么：**只读检查容器、HTTP→HTTPS 跳转、证书 IP SAN、安全响应头、健康接口、数据目录、`8000` 监听范围，以及下载源码包 SHA-256 和镜像 ID。脚本不会登录业务账号或创建任务。

```bash
cd /opt/automation-center
APP_IP=192.168.200.11 APP_DIR=/opt/automation-center sh deploy/scripts/post-deploy-validate.sh \
  | tee /root/automation-center-post-deploy.txt
```

预期结果：所有检查为 `PASS`，结尾 `failures=0`，并打印 `SOURCE_ARCHIVE_SHA256` 和 `IMAGE_ID`。

再从 `192.168.200.0/24` 内另一台运维机验证：

```bash
curl -I http://192.168.200.11:8080/api/v1/health/live
curl -kfsS https://192.168.200.11:8443/api/v1/health/live
curl -kfsS https://192.168.200.11:8443/api/v1/health/ready
```

预期结果：HTTP 返回 `308` 并跳转 8443；两个 HTTPS 接口返回 200。`ready` 中 `salt_mode` 为 `http`。注意：Ready 只验证 SQLite 和配置模式，不替代第 5.4 节真实 Salt 检查。

## 10. 首次登录

**这一步做什么：**验证 Web 页面管理员这一条认证链，与 Salt eAuth 验证分开。浏览器不直接使用 `automation-center` 用户，也不会直接访问 salt-api。

浏览器打开 `https://192.168.200.11:8443`，人工接受或导入自签名证书，然后使用
固定测试账号 `admin/admin` 登录。首次环境变量只在空数据库创建账号时生效；如果
数据库此前已初始化，必须使用第 12 节的 CLI 重置账号后再登录。

登录后按 [`acceptance-192.168.200.11-12.md`](acceptance-192.168.200.11-12.md) 完成真实业务闭环。

## 11. 日常更新和停止

### 11.1 下载新源码包并重建

**这一步做什么：**不使用 `git pull`。先备份 SQLite，再把新压缩包解压到旁路目录，复制现有 `.env` 和 TLS 文件，保留旧源码目录后切换。`/var/lib/automation-center` 是独立持久化目录，不随源码目录切换。

```bash
docker exec automation-center automation-center backup-db

SOURCE_URL='https://github.com/alexyao815/auto/archive/refs/heads/main.tar.gz'
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
NEW_ARCHIVE="/root/automation-center-$timestamp.tar.gz"
NEW_SOURCE_DIR="/opt/automation-center-new-$timestamp"
OLD_SOURCE_DIR="/opt/automation-center-backup-$timestamp"

wget --https-only --max-redirect=20 -O "$NEW_ARCHIVE" "$SOURCE_URL"
sha256sum "$NEW_ARCHIVE" > /root/automation-center-source.sha256.new
tar -tzf "$NEW_ARCHIVE" | sed -n '1,20p'
install -d -m 0755 "$NEW_SOURCE_DIR"
tar -xzf "$NEW_ARCHIVE" -C "$NEW_SOURCE_DIR" --strip-components=1
test -f "$NEW_SOURCE_DIR/docker-compose.yml"
install -m 0600 /opt/automation-center/.env "$NEW_SOURCE_DIR/.env"
install -d -m 0755 "$NEW_SOURCE_DIR/certs"
install -m 0600 /opt/automation-center/certs/tls.key "$NEW_SOURCE_DIR/certs/tls.key"
install -m 0644 /opt/automation-center/certs/tls.crt "$NEW_SOURCE_DIR/certs/tls.crt"

mv /opt/automation-center "$OLD_SOURCE_DIR"
mv "$NEW_SOURCE_DIR" /opt/automation-center
cd /opt/automation-center
docker compose config >/dev/null
docker compose build --pull
docker compose up -d
mv /root/automation-center-source.sha256.new /root/automation-center-source.sha256
printf '%s\n' "$SOURCE_URL" > /root/automation-center-source-url.txt
APP_IP=192.168.200.11 sh deploy/scripts/post-deploy-validate.sh
```

预期结果：新镜像 Healthy，部署后验证通过；旧源码、旧 `.env` 和旧证书仍保存在 `$OLD_SOURCE_DIR`，可用于回退。迁移前应用还会自动使用 SQLite Backup API 再备份一次数据库。

停止条件：新包无法列出、关键文件缺失、复制配置失败、构建失败或容器不 Healthy。切换后失败时不要删除 `$OLD_SOURCE_DIR`；将失败目录移走、把 `$OLD_SOURCE_DIR` 恢复为 `/opt/automation-center`，再用旧镜像启动。

### 11.2 停止与启动

**这一步做什么：**只停止或启动应用容器。Salt Master、salt-api、Minion 和宿主机持久化数据不随容器停止而删除。

```bash
cd /opt/automation-center
docker compose stop
docker compose start
docker compose ps
```

允许使用 `docker compose down` 重建容器，但**禁止使用 `docker compose down -v`**，也禁止删除 `/var/lib/automation-center`。当前 Compose 使用宿主机目录，`-v` 不应作为任何运维流程的一部分。

## 12. 备份与账号重置

**这一步做什么：**`backup-db` 使用 SQLite Backup API 创建一致性备份；`reset-password` 只重置 Web 管理员 `admin`，不会修改 Salt eAuth 用户、App Secret 或 TLS 证书。

手工备份数据库：

```bash
docker exec automation-center automation-center backup-db
find /var/lib/automation-center/backups -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' | sort
```

重置固定账号会使现有 Session 全部失效。避免把密码写入 shell 历史：

```bash
read -r -s -p 'New admin password: ' NEW_ADMIN_PASSWORD; echo
docker exec automation-center automation-center reset-password \
  --username admin --password "$NEW_ADMIN_PASSWORD"
unset NEW_ADMIN_PASSWORD
```

Salt API 密码轮换必须同时更新 `/etc/salt/api-users` 和 `.env`/数据库 Settings；若页面曾保存过 Salt credential，数据库值会在重启时覆盖 `.env`，应先在“系统设置”更新再重启。

## 13. 故障诊断与回滚

### 13.1 容器日志和健康状态

**这一步做什么：**确认故障发生在容器生命周期、健康检查，还是应用启动日志；这些命令只读，不会重启服务。

```bash
docker compose -f /opt/automation-center/docker-compose.yml ps
docker compose -f /opt/automation-center/docker-compose.yml logs --tail=300 automation-center
docker inspect automation-center --format '{{json .State}}'
```

### 13.2 Salt 日志

**这一步做什么：**把应用故障与 Salt 服务、Minion 身份和在线状态分开检查。这里的 Minion ID/Key 与 Web 管理员无关。

```bash
journalctl -u salt-master -u salt-api -n 300 --no-pager
salt-key -L
salt "$MINION_ID" test.ping
```

### 13.3 数据边界

**这一步做什么：**确认数据库、包、日志、工作目录和备份仍位于宿主机持久化目录，避免把容器内临时文件误当成真实数据。

```bash
find /var/lib/automation-center -maxdepth 2 -printf '%M %u:%g %p\n' | sort
du -sh /var/lib/automation-center/*
```

不要直接编辑 SQLite，不要手工删除活跃任务的 Package、日志或远端工作目录。

### 13.4 Salt 配置回滚

**这一步做什么：**只恢复第 5.1 节备份的 Salt Master 配置和 eAuth 摘要文件，用于撤销本次 Salt 权限改动。它不会恢复 `.env`、App Secret、TLS 证书或 Automation Center 数据库。

```bash
backup_dir="$(cat /root/automation-center-salt-backup-path.txt)"
test -d "$backup_dir"
failed_dir="/root/salt-master.d-failed-$(date -u +%Y%m%dT%H%M%SZ)"
cp -a "$backup_dir/master" /etc/salt/master
mv /etc/salt/master.d "$failed_dir"
cp -a "$backup_dir/master.d" /etc/salt/master.d
if [ -f "$backup_dir/api-users" ]; then
  cp -a "$backup_dir/api-users" /etc/salt/api-users
fi
systemctl restart salt-master salt-api
systemctl is-active salt-master salt-api
```

执行回滚前必须确认 `backup_dir` 是第 5.1 节生成的精确绝对路径。上述操作只恢复 Salt 配置，不删除 Automation Center 数据。

## 14. 部署完成标准

- 两次预检均 `failures=0`，部署后验证 `failures=0`。
- 专用 `automation-center` eAuth 通过全部最小能力验证，旧 `automation` 调用不受影响。
- `8000` 只监听回环；SELinux 为 `Disabled`、firewalld 未运行；部署者已确认这是隔离测试网或已有上游访问控制。
- 容器 Healthy，HTTPS、自签名 IP SAN、安全头、SQLite Ready 均通过。
- 下载 URL、源码压缩包 SHA-256、image ID、Salt 备份路径和测试证据已保存。
- 真实业务验收按下一份文档执行，未执行项保持 `NOT RUN`。
