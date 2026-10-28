# Container image. Mount your own knowledge and policy over /app/knowledge and /app/policy;
# the image ships the synthetic examples so it runs out of the box.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 --home-dir /var/lib/governed-metrics app \
    && install -d -o app -g app -m 0700 /var/lib/governed-metrics

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache
COPY examples/knowledge ./knowledge
COPY examples/access.yaml examples/access.grants.yaml ./policy/

# An image has no .git, so the knowledge version is stamped at build time.
ARG KNOWLEDGE_VERSION=unknown
ENV GM_KNOWLEDGE_VERSION=${KNOWLEDGE_VERSION} \
    GM_KNOWLEDGE_PATH=/app/knowledge \
    GM_POLICY_PATH=/app/policy/access.yaml \
    GM_OVERLAY_PATH=/var/lib/governed-metrics/overlay.jsonl \
    GM_AUDIT_LOG=/var/lib/governed-metrics/audit.jsonl \
    GM_TRANSPORT=streamable-http \
    GM_HOST=0.0.0.0 \
    GM_ALLOW_NON_LOOPBACK_BIND=1 \
    GM_PORT=8080

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"
CMD ["governed-metrics"]
