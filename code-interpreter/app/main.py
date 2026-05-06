from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version as _package_version
from shutil import which
from typing import Final

from fastapi import FastAPI

from app.api.routes import router as api_router
from app.app_configs import EXECUTOR_BACKEND, HOST, PORT, PYTHON_EXECUTOR_DOCKER_IMAGE
from app.models.schemas import HealthResponse
from app.services.executor_factory import get_executor

SESSION_REAPER_INTERVAL_SEC = 30

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

SERVICE_VERSION: Final[str] = _package_version("code-interpreter")


def _ensure_docker_image_available() -> None:
    """Ensure the Docker executor image is available locally.

    This checks if the image exists locally, and if not, attempts to pull it.
    This runs during application startup to ensure the image is ready before
    accepting requests.
    """
    docker_bin = which("docker")
    if not docker_bin:
        logger.warning("Docker binary not found, skipping image check")
        return

    image_with_tag = f"{PYTHON_EXECUTOR_DOCKER_IMAGE}:latest"

    # Check if image exists locally
    logger.info(f"Checking for Docker image: {image_with_tag}")
    check_result = subprocess.run(
        [docker_bin, "image", "inspect", image_with_tag],
        capture_output=True,
        timeout=10,
        check=False,
    )

    if check_result.returncode == 0:
        logger.info(f"Docker image {image_with_tag} is already available locally")
        return

    # Image doesn't exist, try to pull it
    logger.info(f"Docker image {image_with_tag} not found locally, attempting to pull...")
    try:
        pull_result = subprocess.run(
            [docker_bin, "pull", image_with_tag],
            capture_output=True,
            timeout=300,  # 5 minutes timeout for pulling
            check=False,
        )

        if pull_result.returncode == 0:
            logger.info(f"Successfully pulled {image_with_tag}")
        else:
            error_msg = (
                pull_result.stderr.decode("utf-8", errors="replace") if pull_result.stderr else ""
            )
            logger.error(f"Failed to pull {image_with_tag}: {error_msg}")
            raise RuntimeError(
                f"Docker executor image {image_with_tag} is not available locally "
                f"and could not be pulled. Error: {error_msg}"
            )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Timeout while pulling Docker image {image_with_tag}. "
            "This may indicate network issues or the image is very large."
        ) from e


async def _reap_expired_sessions_once() -> None:
    """Run a single reap pass via the configured executor."""
    try:
        count = await asyncio.to_thread(get_executor().reap_expired_sessions)
    except Exception:
        logger.warning("Session reaper pass failed", exc_info=True)
        return
    if count > 0:
        logger.info("Reaped %d expired session(s)", count)


async def _session_reaper_loop() -> None:
    """Periodically delete sessions whose TTL has elapsed."""
    while True:
        await asyncio.sleep(SESSION_REAPER_INTERVAL_SEC)
        await _reap_expired_sessions_once()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan events."""
    # Startup: Ensure Docker executor image is available before accepting requests
    if EXECUTOR_BACKEND == "docker":
        logger.info("Ensuring Docker executor image is available...")
        _ensure_docker_image_available()
        logger.info("Docker executor image is ready")

    # Reap any sessions whose TTL elapsed while the service was down.
    await _reap_expired_sessions_once()
    reaper_task = asyncio.create_task(_session_reaper_loop())

    try:
        yield
    finally:
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task


def create_app() -> FastAPI:
    app = FastAPI(
        title="Code Interpreter API",
        version=SERVICE_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> HealthResponse:
        """Health check that verifies the executor backend is operational."""
        result = get_executor().check_health()
        return HealthResponse(
            status=result.status,
            message=result.message,
            version=SERVICE_VERSION,
        )

    app.include_router(api_router, prefix="/v1")
    return app


app: Final[FastAPI] = create_app()


def run() -> None:
    """Run the API using Uvicorn.

    This is for local/dev usage. Production deployments should use a process manager
    and configure workers according to their environment.
    """
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
