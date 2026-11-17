FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    DB_PATH=/data/mediafinder.db

RUN useradd --system --uid 10001 --no-create-home mediafinder \
    && mkdir /data && chown 10001:10001 /data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY mediafinder ./mediafinder

USER 10001
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080'), timeout=3)"

CMD ["sh", "-c", "exec uvicorn mediafinder.app:app_factory --factory --host 0.0.0.0 --port ${PORT}"]
