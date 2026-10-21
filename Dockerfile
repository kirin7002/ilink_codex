# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.163.0
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm install --global --strict-ssl=true "@openai/codex@${CODEX_VERSION}"

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/app
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    if [ -f /run/secrets/proxy_ca ]; then \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
      apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends git ca-certificates; \
    else apt-get update && apt-get install -y --no-install-recommends git ca-certificates; fi && \
    rm -rf /var/lib/apt/lists/*
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex && \
    ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    useradd --uid 1000 --create-home --shell /bin/bash app && \
    mkdir -p /data/state /data/work /home/app/.codex && \
    chown -R app:app /data /home/app && chmod 700 /data/state /data/work /home/app/.codex
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --no-deps .
COPY --chmod=644 deploy/docker-config.toml /app/config.toml
COPY --chmod=755 deploy/sandbox-check.py deploy/import-auth.py /app/deploy/
USER app
ENTRYPOINT ["ilink-codex", "--config", "/app/config.toml"]
CMD ["worker"]

FROM runtime AS test
COPY --chown=app:app tests /app/tests
ENTRYPOINT ["python", "-m", "unittest", "discover", "-s", "/app/tests", "-v"]
