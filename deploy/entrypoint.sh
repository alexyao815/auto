#!/bin/sh
set -eu

# 首次账号和应用密钥是安全启动前提，容器不能静默使用弱默认值。
for variable in AUTOMATION_CENTER_INITIAL_USERNAME AUTOMATION_CENTER_INITIAL_PASSWORD AUTOMATION_CENTER_APP_SECRET; do
  eval "value=\${$variable:-}"
  if [ -z "$value" ]; then
    echo "missing required environment variable: $variable" >&2
    exit 1
  fi
done

if [ "$AUTOMATION_CENTER_APP_SECRET" = "development-only-change-me" ]; then
  echo "AUTOMATION_CENTER_APP_SECRET must not use the development default" >&2
  exit 1
fi

# 应用写数据库、包、日志和备份；Nginx 只获得上传临时目录的写权限。
mkdir -p /var/lib/automation-center/db /var/lib/automation-center/packages /var/lib/automation-center/temp/nginx /var/lib/automation-center/logs /var/lib/automation-center/work /var/lib/automation-center/backups
chown -R automation:automation /var/lib/automation-center
chown -R www-data:www-data /var/lib/automation-center/temp/nginx
test -r /run/secrets/tls.crt
test -r /run/secrets/tls.key
# supervisord 以前台 PID 1 同时托管单 Worker Uvicorn 与 Nginx。
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/automation-center.conf
