import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_installs_lightweight_workspace_tools() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base_stage = dockerfile.split("FROM python:3.14-bookworm AS base", maxsplit=1)[1]
    install_step = re.search(
        r"apt-get install -y --no-install-recommends \\\n(?P<packages>.*?)"
        r"    && mkdir -p /etc/apt/keyrings",
        base_stage,
        flags=re.DOTALL,
    )

    assert install_step is not None
    packages = set(re.findall(r"[a-z0-9][a-z0-9+.-]*", install_step["packages"]))
    assert {"jq", "less", "nano", "ripgrep", "vim-tiny"} <= packages
    assert "ln -s /usr/bin/vim.tiny /usr/local/bin/vim" in base_stage
