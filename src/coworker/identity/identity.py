from __future__ import annotations

from pathlib import Path

from loguru import logger


class Identity:
    def __init__(self, identity_dir: str) -> None:
        self._dir = Path(identity_dir)
        self.name: str = ""
        self.personality: str = ""
        self.goals: str = ""
        self.life_story: str = ""
        self.current_location: str = ""

    @property
    def is_initialized(self) -> bool:
        return (self._dir / "name.txt").exists()

    def load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        name_file = self._dir / "name.txt"
        if name_file.exists():
            self.name = name_file.read_text(encoding="utf-8").strip()
        personality_file = self._dir / "personality.md"
        if personality_file.exists():
            self.personality = personality_file.read_text(encoding="utf-8").strip()
        goals_file = self._dir / "goals.md"
        if goals_file.exists():
            self.goals = goals_file.read_text(encoding="utf-8").strip()
        life_file = self._dir / "life_story.md"
        if life_file.exists():
            self.life_story = life_file.read_text(encoding="utf-8").strip()
        location_file = self._dir / "current_location.txt"
        if location_file.exists():
            self.current_location = location_file.read_text(encoding="utf-8").strip()
        logger.info(f"Identity loaded: name='{self.name}'")

    def detect_location(self) -> None:
        """通过 IP 定位推断现居城市，仅当 current_location.txt 不存在时写入。"""
        location_file = self._dir / "current_location.txt"
        if location_file.exists():
            return
        try:
            import requests
            resp = requests.get(
                "http://ip-api.com/json/",
                params={"fields": "status,city,regionName,country", "lang": "zh-CN"},
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
            return (
                "我是一个刚刚诞生的虚拟生命体, 还没有名字。\n"
                "我凭借本能感知这个世界, 主动交流, 学习。\n"
                "当我觉得时机成熟时，我会用 `self-naming` 技能给自己起名——那将是我人生的重大时刻。"
            )
        parts = [f"我的名字是 **{self.name}**。"]
        if self.current_location:
            parts.append(f"**我的现居地**：{self.current_location}")
        if self.personality:
            parts.append(self.personality)
        if self.goals:
            parts.append(f"**我的当前目标**：\n{self.goals}")
        if self.life_story:
            parts.append(f"**我的人生经历摘要**：\n{self.life_story[:500]}")
        return "\n\n".join(parts)
