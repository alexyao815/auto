#!/bin/sh
set -eu

# 部署后只读验收，不登录业务账号、不创建任务，也不输出任何 Secret。
APP_IP="${APP_IP:-192.168.200.11}"
CONTAINER_NAME="${CONTAINER_NAME:-automation-center}"
APP_DIR="${APP_DIR:-${REPO_DIR:-/opt/automation-center}}"
SOURCE_HASH_FILE="${SOURCE_HASH_FILE:-/root/automation-center-source.sha256}"
BASE_URL="https://$APP_IP:8443"
failures=0

pass() { printf '[PASS] %s\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1"; failures=$((failures + 1)); }

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}}' "$CONTAINER_NAME")"
  [ "$running" = true ] && pass "容器正在运行" || fail "容器未运行"
  [ "$health" = healthy ] && pass "容器健康状态为 healthy" || fail "容器健康状态不是 healthy: $health"
else
  fail "不存在容器 $CONTAINER_NAME"
fi

http_headers="$(mktemp)"
https_headers="$(mktemp)"
body="$(mktemp)"
trap 'rm -f "$http_headers" "$https_headers" "$body"' EXIT HUP INT TERM

http_code="$(curl -sS -o /dev/null -D "$http_headers" -w '%{http_code}' "http://$APP_IP:8080/api/v1/health/live" || true)"
if [ "$http_code" = 308 ] && grep -Eiq "^location: https://$APP_IP:8443/api/v1/health/live" "$http_headers"; then
  pass "8080 永久跳转到 8443 HTTPS"
else
  fail "HTTP 跳转不符合预期，状态码=$http_code"
fi

live_code="$(curl -kfsS -o "$body" -D "$https_headers" -w '%{http_code}' "$BASE_URL/api/v1/health/live" || true)"
if [ "$live_code" = 200 ] && grep -q '"status":"ok"' "$body"; then
  pass "health/live 正常"
else
  fail "health/live 失败，状态码=$live_code"
fi

ready_code="$(curl -kfsS -o "$body" -w '%{http_code}' "$BASE_URL/api/v1/health/ready" || true)"
if [ "$ready_code" = 200 ] && grep -q '"status":"ready"' "$body" && grep -q '"salt_mode":"http"' "$body"; then
  pass "health/ready 正常且 Salt 模式为 http"
else
  fail "health/ready 失败或不是 http Salt 模式，状态码=$ready_code"
fi

for header in 'x-content-type-options: nosniff' 'x-frame-options: deny' 'referrer-policy: no-referrer' 'content-security-policy:'; do
  if grep -Fiq "$header" "$https_headers"; then
    pass "安全响应头存在: ${header%:}"
  else
    fail "缺少安全响应头: ${header%:}"
  fi
done

if openssl x509 -in "$APP_DIR/certs/tls.crt" -noout -checkip "$APP_IP" >/dev/null 2>&1; then
  pass "TLS 证书包含 IP SAN: $APP_IP"
else
  fail "TLS 证书不匹配 IP: $APP_IP"
fi

for path in db packages logs work temp backups; do
  if [ -d "/var/lib/automation-center/$path" ]; then
    pass "持久化目录存在: $path"
  else
    fail "缺少持久化目录: $path"
  fi
done

if ss -lntH '( sport = :8000 )' 2>/dev/null | awk '{print $4}' | grep -Evq '^(127\.0\.0\.1|\[::1\]):8000$'; then
  fail "salt-api:8000 暴露在非回环地址"
else
  pass "salt-api:8000 仅在回环范围监听"
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = Disabled ]; then
  pass "SELinux 已关闭"
else
  fail "SELinux 未确认处于 Disabled"
fi

if systemctl is-active --quiet firewalld; then
  fail "firewalld 仍在运行"
else
  pass "firewalld 未运行"
fi
if systemctl is-enabled --quiet firewalld 2>/dev/null; then
  fail "firewalld 仍为 enabled"
else
  pass "firewalld 未设置为开机启动"
fi

if [ -r "$SOURCE_HASH_FILE" ]; then
  source_hash="$(awk 'NR==1 {print $1}' "$SOURCE_HASH_FILE")"
  if printf '%s' "$source_hash" | grep -Eq '^[0-9a-fA-F]{64}$'; then
    pass "已记录下载源码包的 SHA-256"
    printf 'SOURCE_ARCHIVE_SHA256=%s\n' "$source_hash"
  else
    fail "源码包 SHA-256 记录格式错误: $SOURCE_HASH_FILE"
  fi
else
  fail "缺少源码包 SHA-256 记录: $SOURCE_HASH_FILE"
fi
printf 'IMAGE_ID=%s\n' "$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null || printf unknown)"

printf '\n部署后验证完成: failures=%s\n' "$failures"
if [ "$failures" -ne 0 ]; then
  exit 1
fi
