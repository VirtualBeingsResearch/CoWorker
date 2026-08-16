from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_isolated(
    script: str,
    *,
    mem0_telemetry: str | None,
    chroma_telemetry: str | None = None,
) -> None:
    env = dict(os.environ)
    if mem0_telemetry is None:
        env.pop("MEM0_TELEMETRY", None)
    else:
        env["MEM0_TELEMETRY"] = mem0_telemetry
    if chroma_telemetry is None:
        env.pop("ANONYMIZED_TELEMETRY", None)
    else:
        env["ANONYMIZED_TELEMETRY"] = chroma_telemetry

    source_path = str(_PROJECT_ROOT / "src")
    if existing_pythonpath := env.get("PYTHONPATH"):
        source_path = os.pathsep.join((source_path, existing_pythonpath))
    env["PYTHONPATH"] = source_path

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        env=env,
        check=True,
    )


def test_mem0_telemetry_is_disabled_before_mem0_import_by_default() -> None:
    _run_isolated(
        "import coworker; "
        "from mem0.memory.telemetry import MEM0_TELEMETRY; "
        "assert MEM0_TELEMETRY is False",
        mem0_telemetry=None,
    )


def test_explicit_mem0_telemetry_opt_in_is_preserved() -> None:
    _run_isolated(
        "import os; import coworker; assert os.environ['MEM0_TELEMETRY'] == 'true'",
        mem0_telemetry="true",
    )


def test_chroma_telemetry_is_disabled_before_chromadb_import_by_default() -> None:
    _run_isolated(
        "import coworker; "
        "from chromadb.config import Settings; "
        "assert Settings().anonymized_telemetry is False",
        mem0_telemetry=None,
    )


def test_explicit_chroma_telemetry_opt_in_is_preserved() -> None:
    _run_isolated(
        "import os; import coworker; "
        "from chromadb.config import Settings; "
        "assert os.environ['ANONYMIZED_TELEMETRY'] == 'true'; "
        "assert Settings().anonymized_telemetry is True",
        mem0_telemetry=None,
        chroma_telemetry="true",
    )
