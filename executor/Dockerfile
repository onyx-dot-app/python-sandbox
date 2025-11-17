FROM python:3.11-slim

# Install build prerequisites and uv for dependency management
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        gfortran \
        libfreetype6-dev \
        liblapack-dev \
        libopenblas-dev \
        libpng-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /tmp/*

ENV PATH="/root/.local/bin:${PATH}"

# Pre-install the scientific stack via uv using a dedicated lockfile
COPY pyproject.toml uv.lock /tmp/executor/
RUN uv sync --directory /tmp/executor \
        --no-dev \
        --frozen \
    && mv /tmp/executor/.venv /opt/executor-venv \
    && rm -rf /tmp/executor

ENV VIRTUAL_ENV="/opt/executor-venv"
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
