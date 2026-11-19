# KAI Pulse on LangGraph: FastAPI service.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-server.txt ./
RUN pip install -r requirements.txt -r requirements-server.txt

# Only what the service needs: no legacy Orchestrate code, data or tests.
COPY tools/ tools/
COPY kai/ kai/
COPY amiri_regular.ttf amiri_bold.ttf ./

RUN useradd --create-home --uid 10001 kai && mkdir -p /data/files && chown kai /data/files
USER kai

ENV KAI_FILE_ROOT=/data/files
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "--factory", "kai.server.main:build_app", "--host", "0.0.0.0", "--port", "8000"]
