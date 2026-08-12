#!/bin/sh
set -eu
umask 077
: "${SALT_API_USERNAME:?required}"
: "${SALT_API_PASSWORD:?required}"
: "${MINION_ID:?required}"
SALT_API_URL="${SALT_API_URL:-http://127.0.0.1:8000}"

# file eAuth 会对请求中的明文密码计算摘要；把 api-users 中的 64 位摘要当密码
# 传入会形成二次 Hash，必然返回 401。
if printf '%s' "$SALT_API_PASSWORD" | grep -Eq '^[0-9a-fA-F]{64}$'; then
  echo '[FAIL] SALT_API_PASSWORD 必须是明文密码，不能传 /etc/salt/api-users 中的 SHA-256 摘要' >&2
  exit 1
fi

# 登录响应写入 0600 临时文件，避免 401 空响应继续触发 Python JSONDecodeError。
login_response="$(mktemp)"
trap 'rm -f "$login_response"' EXIT HUP INT TERM
login_status="$(curl -sS -o "$login_response" -w '%{http_code}' "$SALT_API_URL/login" -H 'Accept: application/json' \
  --data-urlencode "username=$SALT_API_USERNAME" \
  --data-urlencode "password=$SALT_API_PASSWORD" \
  --data-urlencode 'eauth=file')"
if [ "$login_status" != 200 ]; then
  echo "[FAIL] salt-api 登录失败，HTTP $login_status；请核对明文密码与 /etc/salt/api-users 中的摘要" >&2
  exit 1
fi
token="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["return"][0]["token"])' "$login_response")"
rm -f "$login_response"
trap - EXIT HUP INT TERM

request() {
  curl -fsS "$SALT_API_URL" -H 'Accept: application/json' -H "X-Auth-Token: $token" "$@"
}

check_response() {
  label="$1"
  shift
  response="$(request "$@")"
  printf '%s' "$response" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert "return" in payload
text = json.dumps(payload, ensure_ascii=False).lower()
assert "authorization error" not in text
assert "not permitted" not in text
' >/dev/null
  echo "[PASS] $label"
}

# 仅执行只读查询和无副作用命令；不打印 Token、凭据或完整节点返回值。
check_response 'wheel key.list_all' -d client=wheel -d fun=key.list_all
check_response 'runner jobs.list_jobs' -d client=runner -d fun=jobs.list_jobs
check_response 'local test.ping' -d client=local -d tgt="$MINION_ID" -d fun=test.ping
check_response 'local grains.item' -d client=local -d tgt="$MINION_ID" -d fun=grains.item -d arg=host -d arg=fqdn_ip4
check_response 'local service.get_all' -d client=local -d tgt="$MINION_ID" -d fun=service.get_all
check_response 'local cmd.run' -d client=local -d tgt="$MINION_ID" -d fun=cmd.run -d arg='/usr/bin/printf automation-center-api-check'
check_response 'local cmd.run_all' -d client=local -d tgt="$MINION_ID" -d fun=cmd.run_all -d arg='/usr/bin/true'

async_response="$(request -d client=local_async -d tgt="$MINION_ID" -d fun=test.ping)"
jid="$(printf '%s' "$async_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["return"][0]["jid"])')"
test -n "$jid"
echo "[PASS] local_async test.ping 返回 JID"

# Job 刚提交时允许短暂未进入 cache，最多等待 10 秒。
attempt=0
while [ "$attempt" -lt 10 ]; do
  if request -d client=runner -d fun=jobs.lookup_jid -d jid="$jid" | python3 -c 'import json,sys; data=json.load(sys.stdin)["return"][0]; raise SystemExit(0 if data else 1)' 2>/dev/null; then
    echo "[PASS] runner jobs.lookup_jid"
    echo "salt-api minimum capability check passed"
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 1
done

echo "[FAIL] jobs.lookup_jid 在 10 秒内未返回任务结果" >&2
exit 1
