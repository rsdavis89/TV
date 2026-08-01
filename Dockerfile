FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY tools/ ./tools/

# No VOLUME instruction: hosts that manage their own storage (Railway among
# them) reject Dockerfiles that declare one, and every way of running this
# mounts the data directory explicitly anyway — see docker-compose.yml.
ENV TV_DATA_DIR=/data \
    TV_HOST=0.0.0.0

# The default port. TV_PORT overrides it, and so does PORT, which is what
# hosted platforms inject; the app checks both.
EXPOSE 8484

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;p=os.environ.get('TV_PORT') or os.environ.get('PORT') or '8484';urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz')"

CMD ["python", "-m", "app.main"]
