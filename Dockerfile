FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY tools/ ./tools/

ENV TV_DATA_DIR=/data \
    TV_HOST=0.0.0.0 \
    TV_PORT=8484

VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8484/healthz')"

CMD ["python", "-m", "app.main"]
