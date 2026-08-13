#!/bin/sh
set -eu

# 登录与页面 API 回归脚本。密码只通过环境变量进入 curl，不写日志、不打印 Token。
APP_URL="${APP_URL:-https://192.168.200.11:8443}"
APP_USERNAME="${APP_USERNAME:-admin}"
: "${APP_PASSWORD:?请通过 APP_PASSWORD 环境变量提供 Web 测试密码}"

for command in curl python3 awk mktemp; do
  command -v "$command" >/dev/null 2>&1 || { echo "FAIL missing command: $command" >&2; exit 1; }
done

validation_dir="$(mktemp -d)"
trap 'rm -rf "$validation_dir"' EXIT HUP INT TERM
cookie_jar="$validation_dir/cookies.txt"
login_body="$validation_dir/login.json"

login_metric="$(curl -kfsS --max-time 10 -o "$login_body" -c "$cookie_jar" \
  -w '%{http_code} %{time_total}' \
  -H 'Content-Type: application/json' \
  --data "{\"username\":\"$APP_USERNAME\",\"password\":\"$APP_PASSWORD\"}" \
  "$APP_URL/api/v1/auth/login")"
login_status="${login_metric%% *}"
login_seconds="${login_metric#* }"
[ "$login_status" = 200 ] || { echo "FAIL login HTTP=$login_status" >&2; exit 1; }
awk -v value="$login_seconds" 'BEGIN { exit !(value < 1.0) }' || {
  echo "FAIL login_seconds=$login_seconds expected_lt=1.0" >&2
  exit 1
}
csrf_token="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["csrf_token"])' "$login_body")"
[ -n "$csrf_token" ] || { echo 'FAIL empty csrf token' >&2; exit 1; }
echo "PASS login_seconds=$login_seconds"

pids=""
i=1
while [ "$i" -le 20 ]; do
  (
    curl -kfsS --max-time 5 -o "$validation_dir/me-$i.json" -b "$cookie_jar" \
      -w '%{http_code} %{time_total}' "$APP_URL/api/v1/auth/me" > "$validation_dir/me-$i.metric"
  ) &
  pids="$pids $!"
  i=$((i + 1))
done
parallel_failures=0
for pid in $pids; do
  wait "$pid" || parallel_failures=$((parallel_failures + 1))
done
[ "$parallel_failures" -eq 0 ] || { echo "FAIL auth_me_curl_failures=$parallel_failures" >&2; exit 1; }

max_me_seconds="$(awk '$1 != 200 { failures += 1 } $2 > max { max = $2 } END { if (failures) exit 1; printf "%.6f", max }' "$validation_dir"/me-*.metric)" || {
  echo 'FAIL one or more auth/me requests were not HTTP 200' >&2
  exit 1
}
awk -v value="$max_me_seconds" 'BEGIN { exit !(value < 2.0) }' || {
  echo "FAIL auth_me_max_seconds=$max_me_seconds expected_lt=2.0" >&2
  exit 1
}
echo "PASS auth_me_count=20 max_seconds=$max_me_seconds"

for endpoint in dashboard/summary dashboard/recent-tasks nodes packages tasks settings 'audit-logs?limit=20'; do
  metric="$(curl -kfsS --max-time 5 -o /dev/null -b "$cookie_jar" -w '%{http_code} %{time_total}' "$APP_URL/api/v1/$endpoint")"
  status="${metric%% *}"
  seconds="${metric#* }"
  [ "$status" = 200 ] || { echo "FAIL endpoint=$endpoint HTTP=$status" >&2; exit 1; }
  awk -v value="$seconds" 'BEGIN { exit !(value < 2.0) }' || {
    echo "FAIL endpoint=$endpoint seconds=$seconds expected_lt=2.0" >&2
    exit 1
  }
  echo "PASS endpoint=$endpoint seconds=$seconds"
done

refresh_metric="$(curl -kfsS --max-time 10 -o "$validation_dir/nodes-refresh.json" -b "$cookie_jar" \
  -w '%{http_code} %{time_total}' -X POST -H "X-CSRF-Token: $csrf_token" "$APP_URL/api/v1/nodes/refresh")"
refresh_status="${refresh_metric%% *}"
refresh_seconds="${refresh_metric#* }"
[ "$refresh_status" = 200 ] || { echo "FAIL nodes_refresh HTTP=$refresh_status" >&2; exit 1; }
awk -v value="$refresh_seconds" 'BEGIN { exit !(value < 10.0) }' || {
  echo "FAIL nodes_refresh_seconds=$refresh_seconds expected_lt=10.0" >&2
  exit 1
}
echo "PASS nodes_refresh seconds=$refresh_seconds"

curl -kfsS --max-time 5 -o /dev/null -b "$cookie_jar" -X POST \
  -H "X-CSRF-Token: $csrf_token" "$APP_URL/api/v1/auth/logout"
echo 'PASS logout'
echo 'authentication and page API validation passed'
