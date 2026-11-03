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
    ai: str  # "gemini" или "claude"
    model: str
    db_path: str
    gemini_api_key: str = ""
    currency: str = "₸"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        env = os.environ.get
        token = env("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

        ai = env("AI", "gemini").strip().lower()
        if ai == "gemini":
            if not env("GEMINI_API_KEY", "").strip():
                raise SystemExit("AI=gemini, но не задан GEMINI_API_KEY (см. .env.example)")
            model = env("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
        elif ai == "claude":
            if not env("ANTHROPIC_API_KEY", "").strip():
                raise SystemExit("AI=claude, но не задан ANTHROPIC_API_KEY (см. .env.example)")
            model = env("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"
        else:
            raise SystemExit(f"AI={ai}: допустимо только gemini или claude")

        admins = frozenset(
            int(x) for x in env("ADMIN_IDS", "").replace(" ", "").split(",") if x
        )
        return cls(
            bot_token=token,
            admin_ids=admins,
            ai=ai,
            model=model,
            db_path=env("DB_PATH", "").strip() or "zakup.db",
            gemini_api_key=env("GEMINI_API_KEY", "").strip(),
            currency=env("CURRENCY", "").strip() or "₸",
        )
