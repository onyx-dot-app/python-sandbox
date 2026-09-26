# Dockerfile for the Go API service (distinct from executor image)
#
# Base images default to Docker Hardened Images (DHI), pulled directly from
# Docker's dhi.io registry (requires a DHI subscription and `docker login
# dhi.io`); override the build args to use public images instead:
#
#   docker build \
#     --build-arg BUILD_IMAGE=golang:1.26-bookworm \
#     --build-arg RUNTIME_IMAGE=debian:bookworm-slim \
#     -t code-interpreter-go .
#
# The -dev runtime variant is required for the default (Docker-in-Docker)
# build because apt installs the Docker daemon at build time. The service
# binary itself needs no shell — the entrypoint bootstrap (including starting
# dockerd for DinD) is built into it.
#
# Multi-platform builds: the runtime stage runs apt for the target
# architecture, so cross-building (e.g. arm64 on an amd64 host) needs QEMU
# binfmt registered once per host ("exec format error" means it is missing):
#
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
#
# For local development, skip all of that and build only your native
# platform:
#   docker buildx bake local
ARG BUILD_IMAGE=dhi.io/golang:1.26-debian13-dev
ARG RUNTIME_IMAGE=dhi.io/debian-base:trixie-dev

# The build stage always runs on the build host's native architecture and
# cross-compiles for the target — Go needs no emulation for this, and it
# avoids running the toolchain under QEMU on multi-platform builds.
FROM --platform=${BUILDPLATFORM} ${BUILD_IMAGE} AS build

USER root
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

ARG TARGETOS
ARG TARGETARCH

COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath -ldflags="-s -w" \
    -o /out/code-interpreter-api ./cmd/code-interpreter-api

FROM ${RUNTIME_IMAGE}

# Toggle off to skip installing the Docker daemon. Unlike the Python image,
# a SKIP_NESTED_DOCKER=1 build still supports Docker-out-of-Docker (mounted
# socket) — the service uses the Engine API directly — so only
# Docker-in-Docker deployments need the default full build.
ARG SKIP_NESTED_DOCKER=0

USER root
WORKDIR /app

# passwd provides groupadd/useradd for the build-time user setup below; both
# packages are present on stock debian images but not guaranteed in hardened
# ones. No shell is required at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends passwd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker's apt signing key, fetched at build time so curl/gnupg are never
# installed in the image.
ADD https://download.docker.com/linux/debian/gpg /etc/apt/keyrings/docker.asc

# Install the Docker daemon if not disabled via build arg. This is needed
# ONLY for Docker-in-Docker (--privileged with no socket mounted): the
# service talks to the daemon through the Engine API socket, so
# Docker-out-of-Docker and Kubernetes deployments need no docker packages at
# all. docker-ce-cli comes along as an apt dependency of docker-ce; the
# service itself never invokes it.
RUN if [ "${SKIP_NESTED_DOCKER}" != "1" ]; then \
    echo "Installing Docker daemon for Docker-in-Docker support"; \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" | \
        tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends iptables docker-ce docker-ce-cli containerd.io && \
    rm -rf /var/lib/apt/lists/*; \
    which docker && docker --version || echo "Docker installation failed"; \
    else \
    rm -f /etc/apt/keyrings/docker.asc; \
    fi

COPY --from=build /out/code-interpreter-api /usr/local/bin/code-interpreter-api

# Create api user with UID 1000 to match Kubernetes security context and optionally add to docker group
RUN groupadd --gid 1000 api \
    && useradd --uid 1000 --gid api --create-home --home-dir /home/api api \
    && chown -R api:api /app \
    && if [ "${SKIP_NESTED_DOCKER}" != "1" ]; then \
        usermod -aG docker api || true; \
    fi

ENV PYTHON_EXECUTOR_DOCKER_IMAGE=onyxdotapp/python-executor-sci

# Don't switch to api user - let the container decide at runtime
# This allows running with --user root when Docker access is needed

EXPOSE 8000

# The binary performs its own Docker bootstrap (mode detection, cgroup v2
# nesting, and starting dockerd for Docker-in-Docker) — no shell entrypoint.
ENTRYPOINT ["/usr/local/bin/code-interpreter-api"]
