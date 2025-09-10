# 0) Берём бинарь mediamtx из официального образа
FROM bluenviron/mediamtx:latest AS mtx

# 1) Наш рантайм
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# На всякий случай включим universe (если вдруг rist-tools не найдётся)
RUN sed -n 'p' /etc/apt/sources.list && \
    apt-get update && apt-get install -y software-properties-common && \
    add-apt-repository universe

# ffmpeg, ristsender, python + pip, curl, certs
RUN apt-get update && apt-get install -y \
    ffmpeg python3 python3-pip curl ca-certificates rist-tools iproute2 tcpdump lsof && \
    rm -rf /var/lib/apt/lists/*

# Кладём бинарь MediaMTX из слоя mtx
COPY --from=mtx /mediamtx /usr/local/bin/mediamtx
RUN chmod +x /usr/local/bin/mediamtx

# Наши файлы
WORKDIR /app
COPY entrypoint.py /app/entrypoint.py
COPY mediamtx.yml   /app/mediamtx.yml
COPY config.example.yml /app/config.yml

# Веб
# ... предыдущие шаги установки ffmpeg, rist-tools, COPY entrypoint.py и т.п.

# Веб + Python окружение
RUN apt-get update && apt-get install -y python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir flask pyyaml

ENV CONFIG_PATH=/data/config.yml \
    WEB_PORT=8081
VOLUME ["/data"]
EXPOSE 8081

ENTRYPOINT ["python3", "/app/entrypoint.py"]

