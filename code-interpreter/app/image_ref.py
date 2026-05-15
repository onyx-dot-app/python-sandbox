"""Helpers for normalizing Docker image references."""

from __future__ import annotations


def normalize_image_ref(ref: str) -> str:
    """Return ``ref`` with an explicit ``:latest`` tag if it has neither tag nor digest.

    Docker image references follow the grammar
    ``[registry[:port]/]repo[:tag|@digest]``. A bare repository
    (``repo``, ``owner/repo``, ``registry.io/owner/repo``) needs an explicit
    tag for some operations such as ``docker image inspect``. References
    that already carry a tag (``repo:v1``) or a digest
    (``repo@sha256:…``) must be returned unchanged — appending ``:latest``
    to either produces an invalid reference.

    Registry ports require care: in ``registry.io:443/owner/repo``, the
    ``:`` is a port separator, not a tag separator. The rule we apply is
    that ``:`` is only a tag separator when it appears after the rightmost
    ``/``.
    """
    if "@" in ref:
        return ref
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        return ref
    return f"{ref}:latest"
