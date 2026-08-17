"""Docker Registry HTTP API V2 client, used only to check for image updates.

Talks directly to the registry (Docker Hub, GHCR, Quay, private registries)
using the standard anonymous Bearer-token flow - no third-party service
involved. Only ever does a HEAD request for a manifest digest, never pulls
image data.
"""

from __future__ import annotations

import logging
import re

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

DEFAULT_REGISTRY = "registry-1.docker.io"
AUTH_REALM_RX = re.compile(r'realm="([^"]+)"')
AUTH_SERVICE_RX = re.compile(r'service="([^"]+)"')
AUTH_SCOPE_RX = re.compile(r'scope="([^"]+)"')

MANIFEST_ACCEPT = ",".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

_TIMEOUT = ClientTimeout(connect=5, sock_connect=5, total=10)


#################################################################
def parse_image_reference(image: str) -> tuple[str, str, str]:
    """Split an image reference into (registry, repository, tag).

    Mirrors Docker's own rules for what counts as a registry host vs. the
    first path segment of a Docker Hub repository.
    """

    ref = image

    registry = DEFAULT_REGISTRY
    first_segment, _, remainder = ref.partition("/")
    if remainder and ("." in first_segment or ":" in first_segment or first_segment == "localhost"):
        registry = first_segment
        ref = remainder

    tag = "latest"
    path, _, last_segment = ref.rpartition("/")
    if ":" in last_segment:
        last_segment, _, tag = last_segment.rpartition(":")
        ref = f"{path}/{last_segment}" if path else last_segment
    elif "@" in last_segment:
        # Pinned by digest already, nothing meaningful to compare against
        last_segment, _, tag = last_segment.rpartition("@")
        ref = f"{path}/{last_segment}" if path else last_segment

    if registry == DEFAULT_REGISTRY and "/" not in ref:
        ref = f"library/{ref}"

    return registry, ref, tag


#################################################################
async def _get_bearer_token(
    session: ClientSession, www_authenticate: str
) -> str | None:
    """Fetch an anonymous Bearer token from the challenge in a 401 response."""

    realm_match = AUTH_REALM_RX.search(www_authenticate)
    if not realm_match:
        return None

    params = {}
    if service_match := AUTH_SERVICE_RX.search(www_authenticate):
        params["service"] = service_match.group(1)
    if scope_match := AUTH_SCOPE_RX.search(www_authenticate):
        params["scope"] = scope_match.group(1)

    async with session.get(
        realm_match.group(1), params=params, timeout=_TIMEOUT
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("token") or data.get("access_token")


#################################################################
async def async_get_remote_digest(session: ClientSession, image: str) -> str | None:
    """Return the current manifest digest for an image tag, or None.

    Only ever performs a HEAD request (no image data is pulled). Returns
    None on any failure - callers should treat that as "unknown", not as
    "no update available".
    """

    try:
        registry, repository, tag = parse_image_reference(image)
        url = f"https://{registry}/v2/{repository}/manifests/{tag}"
        headers = {"Accept": MANIFEST_ACCEPT}

        async with session.head(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status == 401:
                www_authenticate = resp.headers.get("WWW-Authenticate", "")
                token = await _get_bearer_token(session, www_authenticate)
                if not token:
                    return None
                headers["Authorization"] = f"Bearer {token}"
                async with session.head(
                    url, headers=headers, timeout=_TIMEOUT
                ) as resp2:
                    if resp2.status != 200:
                        return None
                    return resp2.headers.get("Docker-Content-Digest")

            if resp.status != 200:
                return None

            return resp.headers.get("Docker-Content-Digest")

    except Exception as err:
        _LOGGER.debug("Failed to check registry for '%s': %s", image, str(err))
        return None
