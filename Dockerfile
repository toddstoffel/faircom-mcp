FROM debian:stable-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FAIRCOM_HTTP_HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       python3 \
       python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python3 -m pip install --break-system-packages .

RUN useradd --system --home-dir /var/lib/faircom-mcp --create-home --shell /usr/sbin/nologin faircom-mcp \
    && mkdir -p /var/log/faircom-mcp /run/faircom-mcp \
    && chown -R faircom-mcp:faircom-mcp /var/lib/faircom-mcp /var/log/faircom-mcp /run/faircom-mcp

EXPOSE 8000

USER faircom-mcp

ENTRYPOINT ["faircom-mcp-server"]
CMD ["--transport", "http"]
