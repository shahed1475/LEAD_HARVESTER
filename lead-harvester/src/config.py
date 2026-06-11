from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    clarioscope_url: str
    harvester_token: str
    harvester_poll_interval_ms: int = Field(default=30_000, ge=1_000)
    proxy_url: str | None = None
    batch_size: int = Field(default=200, ge=1, le=200)
    log_level: str = "INFO"


settings = Settings()
