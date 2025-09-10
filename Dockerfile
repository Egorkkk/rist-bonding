# --- Стадия 1: Сборка librist (для ristsender)
FROM ubuntu:24.04 AS ristbuild
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    git build-essential meson ninja-build pkg-config \
    libmbedtls-dev libcjson-dev libmicrohttpd-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth=1 https://code.videolan.org/rist/librist.git /src/librist && \
    meson setup /src/librist/build --prefix=/usr && \
    ninja -C /src/librist/build && \
    ninja -C /src/librist/build install

# --- Стадия 2: Рантайм
FROM ubuntu:24.04
ARG MEDIAMTX_VERSION="latest"
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ffmpeg python3 python3-pip curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Установка MediaMTX (статический бинарник)
# Пытаемся сначала точную версию, потом latest.
RUN set -eux; \
    if [ "$MEDIAMTX_VERSION" = "latest" ]; then \
      curl -L -o /tmp/mediamtx.tar.gz https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_linux_amd64.tar.gz; \
    else \
      curl -L -o /tmp/mediamtx.tar.gz https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION#v}_linux_amd64.tar.gz || \
      curl -L -o /tmp/mediamtx.tar.gz https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_linux_amd64.tar.gz; \
    fi; \
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx || tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin; \
    chmod +x /usr/local/bin/mediamtx; \
    rm -f /tmp/mediamtx.tar.gz

# Копируем ristsender из стадии сборки
# (в зависимости от инсталляции он может оказаться в /usr/bin или /usr/local/bin)
COPY --from=ristbuild /usr/bin/ristsender /usr/bin/ristsender
COPY --from=ristbuild /usr/local/bin/ristsender /usr/local/bin/ristsender

WORKDIR /app
COPY entrypoint.py /app/entrypoint.py
COPY mediamtx.yml /app/mediamtx.yml
COPY config.example.yml /app/config.yml

RUN pip3 install --no-cache-dir flask pyyaml

ENV CONFIG_PATH=/data/config.yml \
    WEB_PORT=8081
VOLUME ["/data"]
EXPOSE 8081

# Точка входа — Python
ENTRYPOINT ["python3", "/app/entrypoint.py"]
