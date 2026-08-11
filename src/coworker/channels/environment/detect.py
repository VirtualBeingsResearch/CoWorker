"""Detect runtime environment: container, cloud provider, host info.

Pure functions with process-lifetime caching (the container/cloud facts don't
change during a process).  Used both by the ``get_runtime_context`` tool and
the ``[ENVIRONMENT]`` system-prompt section.
"""

from __future__ import annotations

import os
import socket
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def detect_container() -> dict[str, str | bool]:
    """Detect whether we're running inside a container and which runtime.

    Checks, in order of reliability:
    - ``/.dockerenv`` (Docker)
    - ``/run/.containerenv`` (Podman)
    - ``/proc/1/cgroup`` contents (docker/containerd/kubepods)
    - Well-known cloud-metadata env vars
    """
    result: dict[str, str | bool] = {
        "in_container": False,
        "runtime": "",
        "cloud_provider": "",
    }
    if Path("/.dockerenv").exists():
        result["in_container"] = True
        result["runtime"] = "docker"
    elif Path("/run/.containerenv").exists():
        result["in_container"] = True
        result["runtime"] = "podman"

    cgroup = Path("/proc/1/cgroup")
    if cgroup.is_file():
        try:
            text = cgroup.read_text(encoding="utf-8")
            if "docker" in text:
                result["in_container"] = True
                result["runtime"] = "docker"
            elif "containerd" in text:
                result["in_container"] = True
                result["runtime"] = "containerd"
            elif "kubepods" in text:
                result["in_container"] = True
                result["runtime"] = "kubernetes"
        except OSError:
            pass

    cloud = _detect_cloud_provider()
    if cloud:
        result["cloud_provider"] = cloud
    return result


def _detect_cloud_provider() -> str:
    """Check well-known env-var prefixes for cloud providers."""
    cloud_prefixes = {
        "AUTODL": "autodl",
        "ALIYUN": "aliyun",
        "AWS_": "aws",
        "GOOGLE_CLOUD": "gcp",
        "AZURE_": "azure",
    }
    for env_prefix, provider in cloud_prefixes.items():
        for key in os.environ:
            if key.startswith(env_prefix):
                return provider
    return ""


@lru_cache(maxsize=1)
def get_host_info() -> dict[str, str]:
    """Stable host facts: hostname, pid, python version, cwd."""
    import platform

    return {
        "hostname": socket.gethostname(),
        "pid": str(os.getpid()),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
    }


def get_runtime_context() -> dict[str, dict[str, object]]:
    """Combine container + host info into one snapshot."""
    return {
        "container": dict(detect_container()),
        "host": dict(get_host_info()),
    }


def format_runtime_context() -> str:
    """Human-readable one-liner for the system prompt."""
    container = detect_container()
    host = get_host_info()
    parts: list[str] = []
    if container["in_container"]:
        runtime = container.get("runtime") or "container"
        parts.append(f"running inside {runtime}")
        cloud = container.get("cloud_provider")
        if cloud:
            parts.append(f"on {cloud}")
    else:
        parts.append("running on host")
    parts.append(host["platform"])
    return f"{host['hostname']} ({', '.join(parts)})"
