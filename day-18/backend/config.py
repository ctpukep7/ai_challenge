"""Configuration is local to this day. Never expose credential values."""
from dataclasses import dataclass
from pathlib import Path
import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "periodic-weather-collection" / "SKILL.md"
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    db_path: str
    api_key: str
    model: str
    timezone: str


def settings() -> Settings:
    timezone = os.getenv("DAY18_TIMEZONE", "Asia/Omsk")
    ZoneInfo(timezone)
    return Settings(
        os.getenv("DAY18_DB_PATH") or str(ROOT / "data" / "weather.sqlite3"),
        os.getenv("DEEPSEEK_API_KEY", ""),
        os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), timezone,
    )
