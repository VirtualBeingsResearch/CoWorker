"""Command line entry: ``uv run --frozen python -m evals <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.report import write_report
from evals.runner import DEFAULT_RESULTS_DIR, RunOptions, run
from evals.scenario import SUPPORTED_LOCALES, ScenarioError, discover
from evals.workspace import (
    REPO_ROOT,
    ModelTarget,
    base_url_variable,
    read_api_keys,
    read_base_urls,
)

DEFAULT_SCENARIOS = REPO_ROOT / "evals" / "scenarios" / "care"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="run care scenarios against a model")
    run_cmd.add_argument("paths", nargs="*", default=[str(DEFAULT_SCENARIOS)])
    run_cmd.add_argument("--provider", required=True, help="registered provider name")
    run_cmd.add_argument("--model", required=True, help="model id")
    run_cmd.add_argument("--samples", type=int, help="override samples per scenario and locale")
    run_cmd.add_argument("--locale", choices=SUPPORTED_LOCALES, action="append")
    run_cmd.add_argument("--jobs", type=int, default=1, help="samples to run concurrently")
    run_cmd.add_argument(
        "--env-file", type=Path, help="read LLM__*_API_KEY values from this file"
    )
    run_cmd.add_argument(
        "--providers-file", type=Path, help="providers.json copied into every workspace"
    )
    run_cmd.add_argument(
        "--base-url", help="endpoint for a built-in provider (sets LLM__<PROVIDER>_BASE_URL)"
    )
    run_cmd.add_argument("--output", type=Path, default=DEFAULT_RESULTS_DIR)
    run_cmd.add_argument("--baseline", type=Path, help="earlier run directory to compare with")

    check_cmd = commands.add_parser("check", help="validate scenario files without running")
    check_cmd.add_argument("paths", nargs="*", default=[str(DEFAULT_SCENARIOS)])

    report_cmd = commands.add_parser("report", help="rebuild the summary of a run")
    report_cmd.add_argument("run_dir", type=Path)
    report_cmd.add_argument("--baseline", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "report":
            print(write_report(args.run_dir, args.baseline), end="")
            return 0
        scenarios = discover(args.paths)
        if args.command == "check":
            for scenario in scenarios:
                print(f"ok  {scenario.id} v{scenario.version} ({', '.join(scenario.locales)})")
            return 0
    except ScenarioError as error:
        print(f"error: {error}")
        return 2

    base_urls = read_base_urls(args.env_file)
    if args.base_url:
        variable = base_url_variable(args.provider)
        if variable is None:
            print(f"error: --base-url needs a built-in provider, not {args.provider!r}; "
                  "set base_url in --providers-file instead")
            return 2
        base_urls[variable] = args.base_url
    target = ModelTarget(
        provider=args.provider,
        model=args.model,
        providers_file=args.providers_file,
        api_keys=read_api_keys(args.env_file),
        base_urls=base_urls,
    )
    options = RunOptions(
        target=target,
        samples=args.samples,
        locales=tuple(args.locale) if args.locale else None,
        jobs=args.jobs,
        results_dir=args.output,
    )
    run_dir = asyncio.run(run(scenarios, options))
    print(write_report(run_dir, args.baseline), end="")
    print(f"results: {run_dir}")
    halted = json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("halted")
    if halted:
        print(f"halted: {halted['status']}: {halted['detail']}")
        return 1
    return 0
