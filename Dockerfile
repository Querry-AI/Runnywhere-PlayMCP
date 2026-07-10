# Runnywhere MCP server — PlayMCP in KC container image (PRD §10)
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Prebuilt Seoul graph + RFS attrs + facilities (etl/ outputs). Without them
# the server falls back to the demo grid — do not ship demo mode to the contest.
COPY data ./data

RUN pip install --no-cache-dir .

ENV PORT=8000
# Deploy-time env (PlayMCP in KC): RUNART_BASE_URL=<public endpoint>,
# WEB_CONCURRENCY=1, RUNART_POOL_WORKERS=2, optional KAKAO_REST_API_KEY.
EXPOSE 8000
CMD ["python", "-m", "runart.server"]
