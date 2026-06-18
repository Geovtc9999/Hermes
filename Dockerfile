FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/data/models

# libgomp1 requis par onnxruntime (fastembed) + CLI Infisical (injection runtime des secrets)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl bash \
    && curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | bash \
    && apt-get update && apt-get install -y --no-install-recommends infisical \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/models

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /app/secrets

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
