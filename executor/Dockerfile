FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# Install build prerequisites
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        iptables \
        libfreetype6-dev \
        liblapack-dev \
        libopenblas-dev \
        libpng-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /tmp/*

# Pre-install the scientific stack via uv using a dedicated lockfile
COPY pyproject.toml uv.lock /tmp/executor/
RUN uv sync --directory /tmp/executor \
        --no-dev \
        --frozen \
    && mv /tmp/executor/.venv /opt/executor-venv \
    && rm -rf /tmp/executor

ENV VIRTUAL_ENV="/opt/executor-venv"
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
