#!/bin/sh
set -eu

# 在安装 Automation Center 前执行只读检查。脚本不会修改防火墙、SELinux、Salt 或容器配置。
MINION_ID="${MINION_ID:?请先执行 salt-key -L，并通过 MINION_ID 指定 192.168.200.12 的实际 Minion ID}"
EXPECTED_MINION_IP="${EXPECTED_MINION_IP:-192.168.200.12}"
REQUIRE_DOCKER="${REQUIRE_DOCKER:-0}"

failures=0
warnings=0

pass() { printf '[PASS] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1"; warnings=$((warnings + 1)); }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }

if [ "$(id -u)" -ne 0 ]; then
  fail "请以 root 执行，以便读取服务、监听端口和 Salt Key 状态"
fi

if [ -r /etc/os-release ] && grep -q '^PLATFORM_ID="platform:el9"' /etc/os-release; then
  pass "操作系统属于 EL9 平台"
else
  fail "目标系统不是已确认的 CentOS Stream/RHEL 兼容 EL9"
fi

if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
  pass "系统时间已同步"
else
  warn "未确认系统时间同步；继续部署前检查 chronyd/timedatectl"
fi

available_kib="$(df -Pk /var/lib 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "${available_kib:-}" ] && [ "$available_kib" -ge 10485760 ]; then
  pass "/var/lib 可用空间不少于 10 GiB"
else
  fail "/var/lib 可用空间不足 10 GiB 或无法读取"
fi

if command -v getenforce >/dev/null 2>&1; then
  selinux_state="$(getenforce)"
  if [ "$selinux_state" = Disabled ]; then
    pass "SELinux 已关闭，符合当前测试环境前提"
  else
    fail "SELinux 当前为 $selinux_state；本部署文件未配置 SELinux bind mount 标签"
  fi
else
  fail "未找到 getenforce，无法确认 SELinux 是否已关闭"
fi

if systemctl is-active --quiet firewalld; then
  fail "firewalld 仍在运行；当前测试部署按 firewalld 已关闭编写"
else
  pass "firewalld 未运行，符合当前测试环境前提"
fi
if systemctl is-enabled --quiet firewalld 2>/dev/null; then
  fail "firewalld 仍为 enabled；重启后可能自动启动"
else
  pass "firewalld 未设置为开机启动"
fi

for service in salt-master salt-api; do
  if systemctl is-active --quiet "$service"; then
    pass "$service 正在运行"
  else
    fail "$service 未运行"
  fi
done

if ss -lntH '( sport = :8000 )' 2>/dev/null | awk '{print $4}' | grep -Eq '^(127\.0\.0\.1|\[::1\]):8000$'; then
  pass "salt-api:8000 至少监听回环地址"
else
  fail "未发现 salt-api 在回环地址监听 8000"
fi
if ss -lntH '( sport = :8000 )' 2>/dev/null | awk '{print $4}' | grep -Evq '^(127\.0\.0\.1|\[::1\]):8000$'; then
  fail "8000 同时监听了非回环地址，必须先收敛暴露范围"
else
  pass "未发现 8000 对非回环地址监听"
fi

for port in 4505 4506; do
  if ss -lntH "( sport = :$port )" 2>/dev/null | grep -q .; then
    pass "Salt Master 端口 $port 正在监听"
  else
    fail "Salt Master 端口 $port 未监听"
  fi
done

for port in 8080 8443; do
  if ss -lntH "( sport = :$port )" 2>/dev/null | grep -q .; then
    fail "部署入口端口 $port 已被占用"
  else
    pass "部署入口端口 $port 可用"
  fi
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  pass "Docker Engine 与 Compose 插件可用"
elif [ "$REQUIRE_DOCKER" = "1" ]; then
  fail "Docker Engine 或 Compose 插件不可用"
else
  warn "Docker 尚未就绪；安装后使用 REQUIRE_DOCKER=1 重新执行本脚本"
fi

accepted_keys="$(salt-key -l acc 2>/dev/null || true)"
if printf '%s\n' "$accepted_keys" | grep -Fxq "$MINION_ID"; then
  pass "Minion Key 已接受: $MINION_ID"
else
  fail "已接受 Key 中不存在 $MINION_ID"
fi

if salt --out=txt "$MINION_ID" test.ping 2>/dev/null | grep -Eq ': *True$'; then
  pass "Minion test.ping 成功"
else
  fail "Minion test.ping 失败"
fi

remote_check='test -x /bin/bash && test -x /usr/bin/python3 && command -v tar >/dev/null && test "$(id -u)" -eq 0 && { test ! -e /var/lib/automation-center/tasks || test -w /var/lib/automation-center/tasks; }'
if salt --out=txt "$MINION_ID" cmd.retcode "$remote_check" python_shell=true 2>/dev/null | grep -Eq ': *0$'; then
  pass "Minion 具备 bash、python3、tar 和任务目录执行前提"
else
  fail "Minion 缺少执行依赖、Salt 执行用户不是 root，或既有任务目录不可写"
fi

minion_ips="$(salt --out=txt "$MINION_ID" grains.get fqdn_ip4 2>/dev/null || true)"
if printf '%s\n' "$minion_ips" | grep -Fq "$EXPECTED_MINION_IP"; then
  pass "Minion grains 包含预期地址 $EXPECTED_MINION_IP"
else
  warn "Minion grains 未显示 $EXPECTED_MINION_IP，请人工确认 Minion ID 与主机映射"
fi

printf '\n预检完成: failures=%s warnings=%s\n' "$failures" "$warnings"
if [ "$failures" -ne 0 ]; then
  exit 1
fi
