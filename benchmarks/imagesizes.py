"""Fetch real compressed image sizes (pull size) from Docker Hub manifests."""

import json
import urllib.request

ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def image_size(repo, tag):
    token = fetch(
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    )["token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT}
    manifest = fetch(f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", headers)

    if "manifests" in manifest:  # multi-arch index: pick linux/amd64
        digest = next(
            m["digest"]
            for m in manifest["manifests"]
            if m["platform"]["os"] == "linux"
            and m["platform"]["architecture"] == "amd64"
        )
        manifest = fetch(
            f"https://registry-1.docker.io/v2/{repo}/manifests/{digest}", headers
        )

    layers = [layer["size"] for layer in manifest["layers"]]
    return sum(layers), layers


for repo, tag in [
    ("onyxdotapp/code-interpreter", "latest"),
    ("library/python", "3.11-slim"),
    ("library/debian", "bookworm-slim"),
    ("library/debian", "trixie-slim"),
]:
    total, layers = image_size(repo, tag)
    layer_mb = ", ".join(f"{s / 1e6:.1f}" for s in sorted(layers, reverse=True))
    print(f"{repo}:{tag}  total={total / 1e6:.1f} MB (compressed)  layers MB: [{layer_mb}]")
