from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version as _package_version
from shutil import which
from typing import Final

import anyio.to_thread
from fastapi import FastAPI, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.routes import router as api_router
from app.app_configs import (
    BACKEND_CHECK_TIMEOUT_SEC,
    EXECUTION_QUEUE_TIMEOUT_SEC,
    EXECUTOR_BACKEND,
    HEALTH_CHECK_INTERVAL_SEC,
    HOST,
    KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN,
    MAX_CONCURRENT_EXECUTIONS,
    PORT,
    PYTHON_EXECUTOR_DOCKER_BIN,
    PYTHON_EXECUTOR_DOCKER_IMAGE,
    PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC,
    PYTHON_EXECUTOR_DOCKER_NETWORK,
)
from app.image_ref import normalize_image_ref
from app.logging_config import setup_logging
from app.models.schemas import HealthResponse
from app.services.admission import ExecutionLimiter
from app.services.executor_base import HealthCheck
from app.services.executor_factory import get_executor

SESSION_REAPER_INTERVAL_SEC = 30

# Configure logging
setup_logging()

logger = logging.getLogger(__name__)

SERVICE_VERSION: Final[str] = _package_version("code-interpreter")

# Threads left for non-execution work (file routes, stream reads) on top of
# one thread per admitted execution.
_THREADPOOL_HEADROOM: Final[int] = 24


def network_isolation_mode() -> str:
    """Describe how executor sandboxes are cut off from the network."""
    if EXECUTOR_BACKEND.lower() == "kubernetes":
        if KUBERNETES_EXECUTOR_NET_ADMIN_LOCKDOWN:
            return "net_admin_init_container+network_policy"
        return "network_policy_only"
    return f"docker_network:{PYTHON_EXECUTOR_DOCKER_NETWORK}"


async def _check_backend() -> HealthCheck:
    """Run the executor health check off the request threadpool, with a timeout."""
    try:
        with anyio.fail_after(BACKEND_CHECK_TIMEOUT_SEC):
            return await asyncio.to_thread(lambda: get_executor().check_health())
    except TimeoutError:
        return HealthCheck(
            status="error",
            message=f"Executor backend check timed out after {BACKEND_CHECK_TIMEOUT_SEC}s",
        )
    except Exception as e:
        return HealthCheck(status="error", message=f"Executor backend check failed: {e}")


async def _refresh_backend_health(app: FastAPI) -> HealthCheck:
    result = await _check_backend()
    app.state.backend_health = result
    return result


async def _backend_health_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SEC)
        await _refresh_backend_health(app)


def _docker_image_present(docker_bin: str, image_with_tag: str) -> bool:
    """Return whether ``image_with_tag`` exists in the local image store."""
    result = subprocess.run(
        [docker_bin, "image", "inspect", image_with_tag],
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def _pull_docker_image(docker_bin: str, image_with_tag: str) -> None:
    """Pull ``image_with_tag``. Raises ``RuntimeError`` if the pull fails or times out."""
    try:
        pull_result = subprocess.run(
            [docker_bin, "pull", image_with_tag],
            capture_output=True,
            timeout=300,  # 5 minutes timeout for pulling
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Timeout while pulling Docker image {image_with_tag}. "
            "This may indicate network issues or the image is very large."
        ) from e

    if pull_result.returncode == 0:
        logger.info(f"Successfully pulled {image_with_tag}")
        return

    error_msg = pull_result.stderr.decode("utf-8", errors="replace") if pull_result.stderr else ""
    logger.error(f"Failed to pull {image_with_tag}: {error_msg}")
    raise RuntimeError(
        f"Docker executor image {image_with_tag} is not available locally "
        f"and could not be pulled. Error: {error_msg}"
    )


def _ensure_docker_image_available() -> None:
    """Ensure the Docker executor image is available locally.

    This checks if the image exists locally, and if not, attempts to pull it.
    This runs during application startup to ensure the image is ready before
    accepting requests.
    """
    docker_bin = which(PYTHON_EXECUTOR_DOCKER_BIN)
    if not docker_bin:
        logger.warning("Docker binary not found, skipping image check")
        return

    image_with_tag = normalize_image_ref(PYTHON_EXECUTOR_DOCKER_IMAGE)

    logger.info(f"Checking for Docker image: {image_with_tag}")
    if _docker_image_present(docker_bin, image_with_tag):
        logger.info(f"Docker image {image_with_tag} is already available locally")
        return

    logger.info(f"Docker image {image_with_tag} not found locally, attempting to pull...")
    _pull_docker_image(docker_bin, image_with_tag)


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


def _restore_docker_image_if_missing() -> None:
    """Re-pull the executor image if the host has removed it since startup.

    Executor containers start with ``--pull never``, so an image removed by the
    host (``docker system prune -a``, image garbage collection) would otherwise
    fail every execution until an operator restarts the service. The common
    case, image present, costs one ``docker image inspect`` and logs nothing.
    """
    docker_bin = which(PYTHON_EXECUTOR_DOCKER_BIN)
    if not docker_bin:
        return  # Startup already warned, and /health reports this case on its own.

    image_with_tag = normalize_image_ref(PYTHON_EXECUTOR_DOCKER_IMAGE)
    if _docker_image_present(docker_bin, image_with_tag):
        return

    logger.warning(
        "Executor image %s is missing from the host (e.g. removed by `docker system prune`); "
        "re-pulling",
        image_with_tag,
    )
    _pull_docker_image(docker_bin, image_with_tag)


async def _image_watchdog_once() -> None:
    """Run a single image watchdog pass. Never raises.

    A failed re-pull is logged and retried on the next pass; it must not take
    down a running service that may have live sessions.
    """
    try:
        await asyncio.to_thread(_restore_docker_image_if_missing)
    except Exception:
        logger.warning(
            "Executor image watchdog could not restore the executor image; "
            "will retry on the next pass",
            exc_info=True,
        )


async def _image_watchdog_loop(interval_sec: int) -> None:
    """Periodically re-pull the executor image if the host removed it."""
    while True:
        await asyncio.sleep(interval_sec)
        await _image_watchdog_once()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan events."""
    # Startup: Ensure Docker executor image is available before accepting requests
    if EXECUTOR_BACKEND == "docker":
        logger.info("Ensuring Docker executor image is available...")
        _ensure_docker_image_available()
        logger.info("Docker executor image is ready")

    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = max(
        limiter.total_tokens, app.state.execution_limiter.limit + _THREADPOOL_HEADROOM
    )

    await _refresh_backend_health(app)

    # Reap any sessions whose TTL elapsed while the service was down.
    await _reap_expired_sessions_once()
    background_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_session_reaper_loop()),
        asyncio.create_task(_backend_health_loop(app)),
    ]

    # Keep the executor image present for the lifetime of the service; see
    # _image_watchdog_loop. Interval 0 disables it (e.g. air-gapped hosts).
    if EXECUTOR_BACKEND == "docker" and PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC > 0:
        logger.info(
            "Starting executor image watchdog (interval: %ds)",
            PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC,
        )
        background_tasks.append(
            asyncio.create_task(
                _image_watchdog_loop(PYTHON_EXECUTOR_DOCKER_IMAGE_WATCHDOG_INTERVAL_SEC)
            )
        )

    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="Code Interpreter API",
        version=SERVICE_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.state.execution_limiter = ExecutionLimiter(
        limit=MAX_CONCURRENT_EXECUTIONS,
        queue_timeout_sec=EXECUTION_QUEUE_TIMEOUT_SEC,
    )
    app.state.backend_health = None

    def _health_response(result: HealthCheck | None) -> HealthResponse:
        return HealthResponse(
            status=result.status if result else "ok",
            message=result.message if result else None,
            version=SERVICE_VERSION,
            executor_backend=EXECUTOR_BACKEND.lower(),
            network_isolation=network_isolation_mode(),
        )

    @app.get("/health")
    async def health() -> HealthResponse:
        """Liveness: answers from memory, never calls the executor backend.

        ``status`` reflects the last background backend check, so it can lag
        by up to HEALTH_CHECK_INTERVAL_SEC. The HTTP status is always 200.
        """
        return _health_response(app.state.backend_health)

    @app.get("/ready", responses={503: {"model": HealthResponse}})
    async def ready(response: Response) -> HealthResponse:
        """Readiness: runs a fresh backend check; 503 if the backend is unusable."""
        result = await _refresh_backend_health(app)
        if result.status != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return _health_response(result)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(api_router, prefix="/v1")
    return app


app: Final[FastAPI] = create_app()


def run() -> None:
    """Run the API using Uvicorn.

    This is for local/dev usage. Production deployments should use a process manager
    and configure workers according to their environment.
    """
    import uvicorn

    # log_config=None keeps the logging configured by setup_logging(); otherwise
    # uvicorn would install its own handlers/formatters and bypass our format.
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_config=None)
