from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./ensure.db"
    # Comma-separated list of allowed browser origins.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    seed_demo_data: bool = True
    environment_label: str = "DEMO"
    # Token signing key. If empty, a random key is generated once and stored in backend/.secret_key.
    secret_key: str = ""
    token_ttl_hours: int = 12
    upload_dir: str = str(BACKEND_DIR / "uploads")
    max_upload_mb: int = 5
    # Password for the seeded DEMO staff and demo customer accounts. Never use in production.
    demo_password: str = "demo@12345!"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
