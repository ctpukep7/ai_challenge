from dataclasses import dataclass
from pathlib import Path
import os
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

@dataclass
class Settings:
    db_path: str = str(ROOT / 'data' / 'day20.sqlite')
    timezone: str = 'Asia/Omsk'
    model: str = 'deepseek-flash'
    api_key: str = ''
    base_url: str = 'https://api.deepseek.com'
    calendar_username: str = ''
    calendar_password: str = ''
    calendar_url: str = 'https://caldav.yandex.ru'
    fixture_path: str = ''
    run_timeout: float = 180
    tool_timeout: float = 35

    def __post_init__(self):
        ZoneInfo(self.timezone)
        if self.run_timeout<=0 or self.tool_timeout<=0:raise ValueError('Таймаут должен быть положительным')

    @classmethod
    def from_env(cls):
        # Only the host loads .env; MCP subprocesses never load dotenv.
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
        return cls(db_path=os.getenv('DAY20_DB_PATH', str(ROOT/'data'/'day20.sqlite')),
                   timezone=os.getenv('TIMEZONE', 'Asia/Omsk'), model=os.getenv('DEEPSEEK_MODEL','deepseek-flash'),
                   api_key=os.getenv('DEEPSEEK_API_KEY',''), base_url=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com'),
                   calendar_username=os.getenv('YANDEX_CALDAV_USERNAME',''), calendar_password=os.getenv('YANDEX_CALDAV_APP_PASSWORD',''),
                   calendar_url=os.getenv('YANDEX_CALDAV_URL','https://caldav.yandex.ru'))
