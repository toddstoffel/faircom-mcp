FROM debian:stable-slim AS package-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       build-essential \
       bash \
       ruby \
       ruby-dev \
       rpm \
       python3 \
       python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN gem install --no-document fpm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY devtools ./devtools
COPY packaging ./packaging

RUN PACKAGE_BUILD_MODE=native bash devtools/build_linux_packages.sh


FROM fedora:41 AS package-builder-rpm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN dnf -y update \
    && dnf -y install \
       ca-certificates \
       gcc \
       gcc-c++ \
       make \
       bash \
       ruby \
       ruby-devel \
       rpm-build \
       python3 \
       python3-pip \
       findutils \
     && dnf clean all

RUN gem install --no-document fpm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY devtools ./devtools
COPY packaging ./packaging

# Project version is dynamic in pyproject; read the package version constant directly.
RUN APP_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/faircom_mcp/_version.py | head -n1)" \
    && test -n "$APP_VERSION" \
    && PACKAGE_BUILD_MODE=native bash devtools/build_linux_packages.sh "$APP_VERSION"


FROM python:3.13-alpine AS runtime-deb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FAIRCOM_HTTP_HOST=0.0.0.0

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN apk add --no-cache ca-certificates \
    && python -m pip install --no-cache-dir . \
    && mkdir -p /var/lib/faircom-mcp /var/log/faircom-mcp /run/faircom-mcp \
    && chown -R 10001:10001 /var/lib/faircom-mcp /var/log/faircom-mcp /run/faircom-mcp

EXPOSE 8000

USER 10001:10001

ENTRYPOINT ["faircom-mcp-server"]
CMD ["--transport", "http"]


FROM fedora:41 AS runtime-rpm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FAIRCOM_HTTP_HOST=0.0.0.0

RUN dnf -y update \
    && dnf -y install \
       ca-certificates \
       python3 \
       python3-pip \
       systemd \
       shadow-utils \
    && dnf clean all

COPY --from=package-builder-rpm /app/dist/packages/faircom-mcp-*.noarch.rpm /tmp/faircom-mcp.rpm

RUN rpm -Uvh /tmp/faircom-mcp.rpm \
    && rm -f /tmp/faircom-mcp.rpm \
    && if ! id faircom-mcp >/dev/null 2>&1; then useradd --system --home-dir /var/lib/faircom-mcp --create-home --shell /sbin/nologin faircom-mcp; fi \
    && mkdir -p /var/log/faircom-mcp /run/faircom-mcp \
    && chown -R faircom-mcp:faircom-mcp /var/lib/faircom-mcp /var/log/faircom-mcp /run/faircom-mcp

EXPOSE 8000

USER faircom-mcp

ENTRYPOINT ["faircom-mcp-server"]
CMD ["--transport", "http"]


FROM runtime-deb AS runtime
