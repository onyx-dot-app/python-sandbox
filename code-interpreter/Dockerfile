# Dockerfile for the FastAPI application service (distinct from executor image)
FROM python:3.11-slim

ARG SKIP_NESTED_DOCKER=0
ARG SKIP_IMAGE_PRELOAD=0

WORKDIR /app

# Install uv package manager without relying on pip
RUN set -eux \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Docker (full daemon + CLI) if not disabled via build arg
RUN if [ "${SKIP_NESTED_DOCKER}" != "1" ]; then \
    echo "Installing Docker for Docker-in-Docker and Docker-out-of-Docker support"; \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        iptables \
        supervisor && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" | \
        tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io && \
    rm -rf /var/lib/apt/lists/*; \
    which docker && docker --version || echo "Docker installation failed"; \
    fi

ENV PATH="/root/.local/bin:${PATH}"

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY entrypoint.sh ./entrypoint.sh
# Make entrypoint script executable
RUN chmod +x ./entrypoint.sh

# install dependencies
RUN uv sync --frozen --no-dev

# Create api user with UID 1000 to match Kubernetes security context and optionally add to docker group
RUN groupadd --gid 1000 api \
    && useradd --uid 1000 --gid api --create-home --home-dir /home/api api \
    && chown -R api:api /app \
    && if [ "${SKIP_NESTED_DOCKER}" != "1" ]; then \
        usermod -aG docker api || true; \
    fi

# Ensure docker binary is in PATH and set the explicit path when Docker is installed
ENV PATH="/app/.venv/bin:/usr/bin:${PATH}"

# Set the Docker binary path for the executor_docker.py module when Docker is installed
# Using ENV ensures it's available at runtime
ENV PYTHON_EXECUTOR_DOCKER_BIN=/usr/bin/docker
ENV PYTHON_EXECUTOR_DOCKER_IMAGE=onyxdotapp/python-executor-sci

# Don't switch to api user - let the container decide at runtime
# This allows running with --user root when Docker access is needed

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["code-interpreter-api"]
