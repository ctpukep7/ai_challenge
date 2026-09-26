from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Config:
    data_dir: Path
    deepseek_key: str = ""
    tavily_key: str = ""
    model: str = "deepseek-flash"

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ.get("DAY19_DATA_DIR", str(ROOT / "data"))),
                   os.environ.get("DEEPSEEK_API_KEY", ""),
                   os.environ.get("TAVILY_API_KEY", ""),
                   os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))

    def public(self):
        return {"deepseek": bool(self.deepseek_key), "tavily": bool(self.tavily_key), "model": self.model}
