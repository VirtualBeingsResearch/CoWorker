from __future__ import annotations

from typing import Protocol

from coworker.channels.base import BaseChannel
from coworker.core.registration import RegistrationError


class ChannelManagement(Protocol):
    async def snapshot(self) -> dict[str, object]: ...

    async def execute(
        self,
        command: str,
        payload: dict[str, object],
    ) -> dict[str, object]: ...


class ChannelSettings(Protocol):
    @property
    def config_key(self) -> str: ...

    async def apply(self, config: object) -> None: ...


class ChannelModule(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def channel(self) -> BaseChannel: ...

    @property
    def management(self) -> ChannelManagement | None: ...

    @property
    def settings(self) -> ChannelSettings | None: ...


class ChannelModuleRegistry:
    """Register optional management surfaces contributed by channel modules."""

    def __init__(self) -> None:
        self._module_names: set[str] = set()
        self._management: dict[str, ChannelManagement] = {}
        self._settings: dict[str, ChannelSettings] = {}

    def register(self, module: ChannelModule) -> None:
        issues = self.registration_issues(module)
        if issues:
            raise RegistrationError("channel module", issues)
        self._module_names.add(module.name)
        management = module.management
        if management is not None:
            self._management[module.name] = management
        settings = module.settings
        if settings is None:
            return
        self._settings[module.name] = settings

    def registration_issues(self, module: ChannelModule) -> list[str]:
        issues: list[str] = []
        if not module.name:
            issues.append("module name is required")
        elif module.name in self._module_names:
            issues.append(f"duplicate module name: {module.name}")
        if module.channel.name != module.name:
            issues.append(
                f"module name {module.name!r} does not match "
                f"channel name {module.channel.name!r}"
            )
        settings = module.settings
        if settings is not None and not settings.config_key:
            issues.append("settings config_key is required")
        return issues

    def management_for(self, channel_name: str) -> ChannelManagement | None:
        return self._management.get(channel_name)

    def settings_for(self, channel_name: str) -> ChannelSettings | None:
        return self._settings.get(channel_name)

    def settings_items(self) -> list[tuple[str, ChannelSettings]]:
        return sorted(self._settings.items())

    def hot_reloadable_keys(self) -> set[str]:
        return {settings.config_key for settings in self._settings.values()}

    def names(self) -> list[str]:
        return sorted(self._module_names)
