"""Tests for ``app.image_ref.normalize_image_ref``.

These exercise every shape of Docker image reference we expect operators
to set in ``PYTHON_EXECUTOR_DOCKER_IMAGE``:

* bare repository (legacy default, must get ``:latest`` appended);
* tagged reference (must be returned unchanged);
* digest reference (must be returned unchanged — appending ``:latest``
  produces an invalid reference);
* registry-with-port variants, where a ``:`` before the last ``/`` is a
  port separator and must NOT be mistaken for a tag.
"""

from __future__ import annotations

from app.image_ref import normalize_image_ref


def test_bare_repo_gets_latest() -> None:
    assert normalize_image_ref("python-executor-sci") == "python-executor-sci:latest"


def test_namespaced_bare_repo_gets_latest() -> None:
    assert (
        normalize_image_ref("onyxdotapp/python-executor-sci")
        == "onyxdotapp/python-executor-sci:latest"
    )


def test_registry_bare_repo_gets_latest() -> None:
    assert normalize_image_ref("ghcr.io/owner/repo") == "ghcr.io/owner/repo:latest"


def test_tagged_reference_unchanged() -> None:
    assert normalize_image_ref("python-executor-sci:0.4.0") == "python-executor-sci:0.4.0"
    assert (
        normalize_image_ref("onyxdotapp/python-executor-sci:0.4.0")
        == "onyxdotapp/python-executor-sci:0.4.0"
    )
    assert normalize_image_ref("ghcr.io/owner/repo:v1") == "ghcr.io/owner/repo:v1"


def test_digest_reference_unchanged() -> None:
    digest = (
        "onyxdotapp/python-executor-sci"
        "@sha256:462c2fb0ed8998b75418d7a3f9d7fb75f61ce4c4605a1468436d5af09b9971b8"
    )
    assert normalize_image_ref(digest) == digest


def test_registry_with_port_bare_repo_gets_latest() -> None:
    # A ``:`` BEFORE the last ``/`` is a port separator, not a tag.
    assert (
        normalize_image_ref("registry.example.com:5000/owner/repo")
        == "registry.example.com:5000/owner/repo:latest"
    )


def test_registry_with_port_and_tag_unchanged() -> None:
    ref = "registry.example.com:5000/owner/repo:v2"
    assert normalize_image_ref(ref) == ref


def test_registry_with_port_and_digest_unchanged() -> None:
    ref = "registry.example.com:5000/owner/repo@sha256:abc"
    assert normalize_image_ref(ref) == ref


def test_idempotent_on_already_tagged() -> None:
    # Running the function twice on its own output must be a no-op once the
    # first application has produced a valid tagged reference.
    once = normalize_image_ref("onyxdotapp/python-executor-sci")
    twice = normalize_image_ref(once)
    assert once == twice == "onyxdotapp/python-executor-sci:latest"
