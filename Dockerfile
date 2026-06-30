FROM python:3.13-slim

ENV GM_CONFIG=/config/configuration.yaml \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

COPY goodwe-mqtt.py .
COPY goodwe-mqtt.example.yaml /app/configuration.example.yaml

RUN groupadd --system goodwe \
    && useradd --system --gid goodwe --home-dir /app goodwe \
    && mkdir -p /config \
    && chown -R goodwe:goodwe /app /config

USER goodwe

ENTRYPOINT ["python", "/app/goodwe-mqtt.py"]
