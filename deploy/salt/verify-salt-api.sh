#!/bin/sh
set -eu
: "${SALT_API_USERNAME:?required}"
: "${SALT_API_PASSWORD:?required}"
SALT_API_URL="${SALT_API_URL:-http://127.0.0.1:8000}"

# 登录密码只通过请求正文发送，脚本输出仅保留能力检查结果，不打印 Token。
token="$(curl -fsS "$SALT_API_URL/login" -H 'Accept: application/json' \
  --data-urlencode "username=$SALT_API_USERNAME" \
  --data-urlencode "password=$SALT_API_PASSWORD" \
  --data-urlencode 'eauth=file' | python3 -c 'import json,sys; print(json.load(sys.stdin)["return"][0]["token"])')"

request() {
  curl -fsS "$SALT_API_URL" -H 'Accept: application/json' -H "X-Auth-Token: $token" "$@"
}

# 分别验证 Key、受限 Job 查询和节点探测三个最小权限面。
request -d client=wheel -d fun=key.list_all >/dev/null
request -d client=runner -d fun=jobs.list_jobs >/dev/null
request -d client=local -d tgt='*' -d fun=test.ping >/dev/null
echo "salt-api minimum capability check passed"
