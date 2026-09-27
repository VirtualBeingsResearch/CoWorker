"""Command line entry: ``uv run --frozen python -m evals <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evals.ability import (
    DEFAULT_ABILITIES,
    AbilityOptions,
    discover_suites,
    run_abilities,
)
from evals.report import write_report
from evals.runner import DEFAULT_RESULTS_DIR, RunOptions, run
from evals.scenario import SUPPORTED_LOCALES, ScenarioError, discover
from evals.states import pack as pack_state
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
    _add_model_args(run_cmd)
    run_cmd.add_argument("paths", nargs="*", default=[str(DEFAULT_SCENARIOS)])
    run_cmd.add_argument("--samples", type=int, help="override samples per scenario and locale")
    run_cmd.add_argument("--locale", choices=SUPPORTED_LOCALES, action="append")
    run_cmd.add_argument("--jobs", type=int, default=1, help="samples to run concurrently")
    run_cmd.add_argument("--output", type=Path, default=DEFAULT_RESULTS_DIR)
    run_cmd.add_argument("--baseline", type=Path, help="earlier run directory to compare with")

    ability_cmd = commands.add_parser("abilities", help="run ability suites (raw vs newborn)")
    _add_model_args(ability_cmd)
    ability_cmd.add_argument("paths", nargs="*", default=[str(DEFAULT_ABILITIES)])
    ability_cmd.add_argument("--locale", choices=SUPPORTED_LOCALES, action="append")
    ability_cmd.add_argument(
        "--modes", default="raw,newborn", help="comma list: raw, newborn"
    )
    ability_cmd.add_argument("--judge-provider", default="deepseek")
    ability_cmd.add_argument("--judge-model", default="deepseek-flash")
    ability_cmd.add_argument("--user-provider", default="")
    ability_cmd.add_argument("--user-model", default="")
    ability_cmd.add_argument("--output", type=Path, default=DEFAULT_RESULTS_DIR)

    check_cmd = commands.add_parser("check", help="validate scenario and ability files")
    check_cmd.add_argument("paths", nargs="*", default=[])

    report_cmd = commands.add_parser("report", help="rebuild the summary of a run")
    report_cmd.add_argument("run_dir", type=Path)
    report_cmd.add_argument("--baseline", type=Path)

    states = commands.add_parser("states", help="life-stage snapshots")
    state_cmds = states.add_subparsers(dest="states_command", required=True)
    pack_cmd = state_cmds.add_parser("pack", help="pack a workspace into evals/states/<name>")
    pack_cmd.add_argument("workspace", type=Path)
    pack_cmd.add_argument("name")
    return parser


def _add_model_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--provider", required=True, help="registered provider name")
    command.add_argument("--model", required=True, help="model id")
    command.add_argument(
        "--env-file", type=Path, help="read LLM__*_API_KEY and _BASE_URL values"
    )
    command.add_argument(
        "--providers-file", type=Path, help="providers.json copied into every workspace"
    )
    command.add_argument(
        "--base-url", help="endpoint for a built-in provider (sets LLM__<PROVIDER>_BASE_URL)"
    )


def _target(args: argparse.Namespace) -> ModelTarget:
    base_urls = read_base_urls(args.env_file)
    if getattr(args, "base_url", None):
        variable = base_url_variable(args.provider)
        if variable is None:
            raise ScenarioError(
                f"--base-url needs a built-in provider, not {args.provider!r}; "
                "set base_url in --providers-file instead"
            )
        base_urls[variable] = args.base_url
    return ModelTarget(
        provider=args.provider,
        model=args.model,
        providers_file=args.providers_file,
        api_keys=read_api_keys(args.env_file),
        base_urls=base_urls,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "report":
            print(write_report(args.run_dir, args.baseline), end="")
            return 0
        if args.command == "states":
            dest = pack_state(args.workspace, args.name)
            print(dest)
            return 0
        if args.command == "check":
            paths = args.paths or [str(DEFAULT_SCENARIOS), str(DEFAULT_ABILITIES)]
            care_paths: list[str | Path] = []
            ability_paths: list[str | Path] = []
            for raw in paths:
                path = Path(raw)
                if "abilities" in path.parts:
                    ability_paths.append(path)
                else:
                    care_paths.append(path)
            if care_paths:
                for scenario in discover(care_paths):
                    print(f"ok  {scenario.id} v{scenario.version} ({', '.join(scenario.locales)})")
            if ability_paths:
                for suite in discover_suites(ability_paths):
                    print(
                        f"ok  {suite.id} v{suite.version} {suite.scoring} "
                        f"({', '.join(suite.locales)}, {len(suite.items)} items)"
                    )
            return 0
        target = _target(args)
        if args.command == "abilities":
            judge = ModelTarget(
                provider=args.judge_provider,
                model=args.judge_model,
                api_keys=target.api_keys,
                base_urls=target.base_urls,
            )
            user = None
            if args.user_provider:
                user = ModelTarget(
                    provider=args.user_provider,
                    model=args.user_model or args.judge_model,
                    api_keys=target.api_keys,
                    base_urls=target.base_urls,
                )
            modes = tuple(part.strip() for part in args.modes.split(",") if part.strip())
            run_dir = asyncio.run(
                run_abilities(
                    discover_suites(args.paths),
                    AbilityOptions(
                        target=target,
                        judge=judge,
                        user=user,
                        modes=modes,
                        locales=tuple(args.locale) if args.locale else None,
                        results_dir=args.output,
                    ),
                )
            )
            print((run_dir / "summary.md").read_text(encoding="utf-8"), end="")
            print(f"results: {run_dir}")
            return 0
        options = RunOptions(
            target=target,
            samples=args.samples,
            locales=tuple(args.locale) if args.locale else None,
            jobs=args.jobs,
            results_dir=args.output,
        )
        run_dir = asyncio.run(run(discover(args.paths), options))
        print(write_report(run_dir, args.baseline), end="")
        print(f"results: {run_dir}")
        halted = json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("halted")
        if halted:
            print(f"halted: {halted['status']}: {halted['detail']}")
            return 1
        return 0
    except (ScenarioError, ValueError) as error:
        print(f"error: {error}")
        return 2
