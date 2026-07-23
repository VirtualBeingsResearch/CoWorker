from __future__ import annotations

import argparse
import re
from datetime import date

from bump_version import ROOT, finalize_changelog_section

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark an unreleased changelog version as released."
    )
    parser.add_argument("version")
    parser.add_argument("--date", default=date.today().isoformat(), dest="released_on")
    args = parser.parse_args()

    if not DATE_RE.fullmatch(args.released_on):
        parser.error("--date must use YYYY-MM-DD")
    try:
        date.fromisoformat(args.released_on)
    except ValueError:
        parser.error("--date must be a valid calendar date")

    path = ROOT / "CHANGELOG.md"
    try:
        finalized = finalize_changelog_section(
            path.read_text(encoding="utf-8"), args.version, args.released_on
        )
    except ValueError as error:
        parser.error(str(error))
    path.write_text(finalized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
