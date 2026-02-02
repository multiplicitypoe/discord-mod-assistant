# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV TZ=UTC
ENV HOME=/tmp
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_ONLY_BINARY=:all:
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    apt-get update; \
    if [ "$arch" = "armhf" ]; then \
        apt-get install -y --no-install-recommends \
            ca-certificates \
            tzdata \
            libbrotli1 \
            libbz2-1.0 \
            libatomic1 \
            libc6 \
            libdeflate0 \
            libfreetype6 \
            libgcc-s1 \
            libjbig0 \
            libjpeg62-turbo \
            liblcms2-2 \
            liblerc4 \
            liblzma5 \
            libopenjp2-7 \
            libstdc++6 \
            libtiff6 \
            libwebp7 \
            libwebpdemux2 \
            libwebpmux3 \
            libxcb1 \
            libxau6 \
            libxdmcp6 \
            libzstd1 \
            zlib1g; \
        (apt-get install -y --no-install-recommends libpng16-16 || apt-get install -y --no-install-recommends libpng16-16t64); \
    else \
        apt-get install -y --no-install-recommends \
            ca-certificates \
            tzdata; \
    fi; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN set -eux; \
    extra=""; \
    if [ "$(dpkg --print-architecture)" = "armhf" ]; then \
        extra="--extra-index-url https://www.piwheels.org/simple"; \
    fi; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir --only-binary=:all: $extra -r /app/requirements.txt

RUN python -c "import aiohttp, aiosqlite, discord, dotenv, openai, pydantic; import PIL"

COPY src /app/src

RUN addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

ENV PYTHONPATH=/app/src

USER app

CMD ["python", "-m", "incident_mod_bot.bot"]
