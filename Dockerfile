FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPTICRIP_DATA_DIR=/config

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=1000:1000 opticrip /app/opticrip

RUN useradd --create-home --uid 1000 opticrip \
    && mkdir -p /config /output \
    && chown -R opticrip:opticrip /config /output

USER opticrip

VOLUME ["/config", "/output"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"]

CMD ["python", "-m", "opticrip.web", "--host", "0.0.0.0", "--port", "8080"]
