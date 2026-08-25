# Runnywhere MCP server — PlayMCP in KC container image (PRD §10)
FROM python:3.12-slim

ARG RUNART_RELEASE_SHA=unknown
ARG RUNART_BASE_URL=https://runnywhere-kakaotools.playmcp-endpoint.kakaocloud.io

WORKDIR /app
COPY pyproject.toml ./
COPY LICENSE DATA_LICENSES.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
# Prebuilt Seoul graph + RFS attrs + facilities (etl/ outputs). Without them
# the server falls back to the demo grid — do not ship demo mode to the contest.
COPY data ./data

RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && pip install --no-cache-dir . \
    && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin runart

ENV PORT=8000
ENV HOST=0.0.0.0
ENV RUNART_RELEASE_SHA=${RUNART_RELEASE_SHA}
ENV RUNART_BASE_URL=${RUNART_BASE_URL}
# Deploy-time env (PlayMCP in KC): RUNART_BASE_URL=<public endpoint>,
# KAKAO_JAVASCRIPT_KEY, RUNART_TOKEN_SECRET (32+ chars), RUNART_LEGAL_CONTACT,
# WEB_CONCURRENCY=1,
# RUNART_POOL_WORKERS=2, RUNART_MAX_CONCURRENT_MCP=10,
# RUNART_MAX_QUEUED_MCP=32, RUNART_MCP_QUEUE_TIMEOUT_S=1.0,
# and optional KAKAO_REST_API_KEY.
EXPOSE 8000
USER 10001:10001
CMD ["python", "-m", "runart.server"]
