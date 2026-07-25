from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from coworker.i18n import tr
from coworker.i18n.runtime import browser_locale


class Identity:
    _FILES = {
        "name": "name.txt",
        "personality": "personality.md",
        "current_location": "current_location.txt",
    }

    def __init__(self, identity_dir: str) -> None:
        self._dir = Path(identity_dir)
        self.name: str = ""
        self.personality: str = ""
        self.current_location: str = ""

    @property
    def is_initialized(self) -> bool:
        return (self._dir / "name.txt").exists()

    def load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for attribute, filename in self._FILES.items():
            path = self._dir / filename
            value = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            setattr(self, attribute, value)
        logger.info(f"Identity loaded: name='{self.name}'")

    def update(self, values: Mapping[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for attribute, value in values.items():
            (self._dir / self._FILES[attribute]).write_text(
                value.strip(),
                encoding="utf-8",
            )
        self.load()

    def detect_location(self) -> None:
        """通过 IP 定位推断现居城市，仅当 current_location.txt 不存在时写入。"""
        location_file = self._dir / "current_location.txt"
        if location_file.exists():
            return
        try:
            import requests

            resp = requests.get(
                "http://ip-api.com/json/",
                params={"fields": "status,city,regionName,country", "lang": browser_locale()},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                return
            city = data.get("city", "")
            region = data.get("regionName", "")
            country = data.get("country", "")
            location = " · ".join(p for p in [country, region, city] if p)
            if location:
                location_file.write_text(location, encoding="utf-8")
                self.current_location = location
                logger.info(f"Location detected: {location}")
        except Exception as e:
            logger.debug(f"IP location detection failed: {e}")

    def to_system_prompt_section(self) -> str:
        if not self.is_initialized:
            return tr("identity.uninitialized")
        parts = [tr("identity.name", name=self.name)]
        if self.current_location:
            parts.append(tr("identity.location", location=self.current_location))
        if self.personality:
            parts.append(self.personality)
        return "\n\n".join(parts)
