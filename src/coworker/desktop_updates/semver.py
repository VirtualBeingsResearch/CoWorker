from __future__ import annotations

import re
from functools import total_ordering

_CORE_IDENTIFIER = r"(?:0|[1-9]\d*)"
_PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER_RE = re.compile(
    rf"^(?P<prefix>v)?(?P<major>{_CORE_IDENTIFIER})\."
    rf"(?P<minor>{_CORE_IDENTIFIER})\."
    rf"(?P<patch>{_CORE_IDENTIFIER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_PRERELEASE_IDENTIFIER})*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class SemVerError(ValueError):
    """Raised when a value is not a strict Semantic Version 2.0 string."""


@total_ordering
class SemVer:
    """Strict SemVer 2.0 value with precedence-aware comparison.

    A single lower-case ``v`` prefix is accepted while parsing, but is omitted
    from the normalized string representation. Build metadata is preserved in
    the representation and ignored for precedence, as required by SemVer 2.0.
    """

    __slots__ = ("build", "major", "minor", "patch", "prerelease")

    def __init__(
        self,
        major: int,
        minor: int,
        patch: int,
        prerelease: tuple[str, ...] = (),
        build: tuple[str, ...] = (),
    ) -> None:
        if min(major, minor, patch) < 0:
            raise SemVerError("semantic version core identifiers must be non-negative")
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
        self.build = build

    @classmethod
    def parse(cls, value: str) -> SemVer:
        if not isinstance(value, str):
            raise SemVerError("semantic version must be a string")
        match = _SEMVER_RE.fullmatch(value)
        if match is None:
            raise SemVerError(f"invalid Semantic Version 2.0 value: {value!r}")
        prerelease = match.group("prerelease")
        build = match.group("build")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(prerelease.split(".")) if prerelease else (),
            build=tuple(build.split(".")) if build else (),
        )

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def precedence_key(self) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
        if not self.prerelease:
            prerelease_key: tuple[tuple[int, int | str], ...] = ((2, 0),)
        else:
            prerelease_key = tuple(
                (0, int(identifier)) if identifier.isdigit() else (1, identifier)
                for identifier in self.prerelease
            )
        return (*self.core, prerelease_key)

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += f"-{'.'.join(self.prerelease)}"
        if self.build:
            value += f"+{'.'.join(self.build)}"
        return value

    def __repr__(self) -> str:
        return f"SemVer({str(self)!r})"

    def __hash__(self) -> int:
        return hash((self.core, self.prerelease))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            try:
                other = SemVer.parse(other)
            except SemVerError:
                return False
        if not isinstance(other, SemVer):
            return NotImplemented
        return self.core == other.core and self.prerelease == other.prerelease

    def __lt__(self, other: object) -> bool:
        if isinstance(other, str):
            other = SemVer.parse(other)
        if not isinstance(other, SemVer):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left:
        return 0 if not right else 1
    if not right:
        return -1
    for left_identifier, right_identifier in zip(left, right, strict=False):
        if left_identifier == right_identifier:
            continue
        left_numeric = left_identifier.isdigit()
        right_numeric = right_identifier.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_identifier) < int(right_identifier) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_identifier < right_identifier else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def parse_semver(value: str) -> SemVer:
    return SemVer.parse(value)


def normalize_version(value: str) -> str:
    return str(SemVer.parse(value))
