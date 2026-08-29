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
# and optional KAKAO_REST_API_KEY.
#
# Measured against the 1,000-call gate with the standard-course catalogue:
#   WEB_CONCURRENCY=1                   avg 75ms / p99 ~200-390ms, ~1.6GB.
#                                       2 doubles the pool as well (6 procs,
#                                       2.5GB) and makes p99 erratic.
#   RUNART_POOL_WORKERS=2               1 saves ~480MB at equal latency but
#                                       leaves less room for starts the
#                                       catalogue does not cover.
#   RATE_LIMIT_RPS=200                  20 now throttles the service itself:
#                                       only 9-535 of 1,000 calls got through.
#                                       Per-IP, so set RUNART_TRUST_PROXY_HOPS
#                                       to match the gateway or one proxy IP
#                                       caps everyone.
#   RUNART_MAX_CONCURRENT_ROUTE_EDITS=4 1 rejected 17% of edits with only ten
#                                       people actively drawing; 4 rejects none
#                                       and keeps the tool gate at 93ms.
#   RUNART_MAX_CONCURRENT_MCP=10, RUNART_MAX_QUEUED_MCP=32,
#   RUNART_MCP_QUEUE_TIMEOUT_S=1.0
# Peak RSS under edit load is ~2.0GB; drop RUNART_POOL_WORKERS to 1 if the
# container is capped at 2GB.
EXPOSE 8000
USER 10001:10001
CMD ["python", "-m", "runart.server"]
