# Two stages so the runtime image carries no build tooling.
FROM python:3.11-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir '.[ai,web]'


FROM python:3.11-slim

# Runs unprivileged: nothing here needs root, and the app writes only to /data.
RUN useradd --create-home --uid 10001 parser \
 && mkdir -p /data/jobs \
 && chown -R parser:parser /data

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORK_DIR=/data/jobs \
    URL_PREFIX=/parserIICS

USER parser
WORKDIR /home/parser
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# One worker per core, but a generous timeout: a large export package takes
# tens of seconds to parse and render, and the request waits for it.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "3", "--threads", "2", "--timeout", "300", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "iics_parser.web.app:app"]
