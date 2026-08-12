FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/src \
    AUTOMATION_CENTER_DATA_DIR=/var/lib/automation-center \
    AUTOMATION_CENTER_BACKEND_ROOT=/app/backend
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx supervisor curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home /app --shell /usr/sbin/nologin automation
COPY backend/ /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.lock
COPY --from=frontend-build /build/frontend/dist/ /usr/share/nginx/html/
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/supervisord.conf /etc/supervisor/conf.d/automation-center.conf
COPY deploy/entrypoint.sh /usr/local/bin/automation-center-entrypoint
COPY deploy/automation-center /usr/local/bin/automation-center
RUN chmod 0755 /usr/local/bin/automation-center-entrypoint /usr/local/bin/automation-center \
    && mkdir -p /var/lib/automation-center /run/nginx \
    && chown -R automation:automation /var/lib/automation-center /app
EXPOSE 8080 8443
VOLUME ["/var/lib/automation-center"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -kfsS https://127.0.0.1:8443/api/v1/health/ready || exit 1
ENTRYPOINT ["/usr/local/bin/automation-center-entrypoint"]
