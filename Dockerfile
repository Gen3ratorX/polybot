FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 10001 polybot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=polybot:polybot . .

RUN mkdir -p /data \
    && chown -R polybot:polybot /app /data

USER polybot

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD sh -c 'pgrep -u 10001 -f "scripts/run_daemon.py" >/dev/null'

ENTRYPOINT ["tini", "--"]
CMD ["python", "scripts/run_daemon.py", "--cycles", "0", "--submit"]
