import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Читает простой .env (KEY=VALUE) без перезаписи уже заданных переменных."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    model: str
    db_path: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("Не задан ANTHROPIC_API_KEY (см. .env.example)")
        admins = frozenset(
            int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
        )
        return cls(
            bot_token=token,
            admin_ids=admins,
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip(),
            db_path=os.environ.get("DB_PATH", "zakup.db").strip(),
        )
