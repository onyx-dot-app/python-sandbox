# Build definitions for the code-interpreter-go images.
#
#   docker buildx bake local              # native-platform build of both
#                                         # variants, loaded into `docker images`
#   docker buildx bake slim-local         # just the slim variant
#   docker buildx bake                    # multi-platform build of both variants
#   VERSION=1.2.3 docker buildx bake --push
#
# Multi-platform targets (`full`, `slim`) cross-build the runtime stage, which
# needs QEMU binfmt registered once per host for foreign architectures:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
# CI avoids that by building each platform on a native runner and merging the
# manifests (see .github/workflows/docker-build-push.yml).
#
# The default base images require a DHI subscription and `docker login dhi.io`.
# Without one, override the hardened base images:
#   BUILD_IMAGE=golang:1.26-bookworm RUNTIME_IMAGE=debian:bookworm-slim \
#     docker buildx bake local

variable "REGISTRY" {
  default = "onyxdotapp"
}

variable "IMAGE_NAME" {
  default = "code-interpreter-go"
}

variable "VERSION" {
  default = "dev"
}

# Base image overrides. Empty means "use the Dockerfile defaults" (the DHI
# hardened images), which stay the single source of truth.
variable "BUILD_IMAGE" {
  default = ""
}

variable "RUNTIME_IMAGE" {
  default = ""
}

# When non-empty, release targets drop their tags so CI can push per-platform
# images by digest (native runner per architecture) and merge the manifest
# lists afterwards with `docker buildx imagetools create`.
variable "PUSH_BY_DIGEST" {
  default = ""
}

group "default" {
  targets = ["full", "slim"]
}

group "local" {
  targets = ["full-local", "slim-local"]
}

target "_common" {
  context    = "."
  dockerfile = "Dockerfile"
  args = {
    BUILD_IMAGE   = BUILD_IMAGE != "" ? BUILD_IMAGE : null
    RUNTIME_IMAGE = RUNTIME_IMAGE != "" ? RUNTIME_IMAGE : null
  }
}

# Default image: includes the Docker daemon so every deployment mode works
# out of the box, including Docker-in-Docker.
target "full" {
  inherits  = ["_common"]
  platforms = ["linux/amd64", "linux/arm64"]
  tags = PUSH_BY_DIGEST != "" ? [] : [
    "${REGISTRY}/${IMAGE_NAME}:${VERSION}",
    "${REGISTRY}/${IMAGE_NAME}:latest",
  ]
}

# Slim image: no docker packages at all. Opt-in for Docker-out-of-Docker
# (mounted socket) and Kubernetes deployments.
target "slim" {
  inherits  = ["_common"]
  platforms = ["linux/amd64", "linux/arm64"]
  args = {
    SKIP_NESTED_DOCKER = "1"
  }
  tags = PUSH_BY_DIGEST != "" ? [] : [
    "${REGISTRY}/${IMAGE_NAME}:${VERSION}-slim",
    "${REGISTRY}/${IMAGE_NAME}:latest-slim",
  ]
}

# Local development builds: no `platforms` means the host's native platform,
# so no QEMU or cross toolchain is involved, and the result is loaded straight
# into the local image store.
target "full-local" {
  inherits = ["_common"]
  tags     = ["${IMAGE_NAME}:local"]
  output   = ["type=docker"]
}

target "slim-local" {
  inherits = ["_common"]
  args = {
    SKIP_NESTED_DOCKER = "1"
  }
  tags   = ["${IMAGE_NAME}:local-slim"]
  output = ["type=docker"]
}
