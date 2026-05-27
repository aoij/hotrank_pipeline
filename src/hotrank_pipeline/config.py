from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    postgres_host: str = os.getenv("POSTGRES_HOST", "127.0.0.1")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_db: str = os.getenv("POSTGRES_DB", "hotrank_pipeline")
    postgres_user: str = os.getenv("POSTGRES_USER", "postgres")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "dfq666.")
    raw_dir: str = os.getenv("HOTRANK_RAW_DIR", str(Path(__file__).resolve().parents[2] / "data" / "raw"))
    tophub_news_url: str = os.getenv("TOPHUB_NEWS_URL", "https://tophub.today/c/news?p=1")
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    local_settings_path: str = os.getenv(
        "HOTRANK_LOCAL_SETTINGS_PATH",
        str(Path(__file__).resolve().parents[2] / "local_settings.json"),
    )
    example_settings_path: str = str(Path(__file__).resolve().parents[2] / "local_settings.example.json")

    @property
    def dsn(self) -> str:
        return (
            f"host={self.postgres_host} "
            f"port={self.postgres_port} "
            f"dbname={self.postgres_db} "
            f"user={self.postgres_user} "
            f"password={self.postgres_password}"
        )


def get_settings() -> Settings:
    return Settings()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_runtime_config(settings: Settings) -> dict[str, Any]:
    example_path = Path(settings.example_settings_path)
    local_path = Path(settings.local_settings_path)

    base: dict[str, Any] = {}
    if example_path.exists():
        base = json.loads(example_path.read_text(encoding="utf-8"))

    if local_path.exists():
        override = json.loads(local_path.read_text(encoding="utf-8"))
        return _deep_merge(base, override)

    return base


def save_runtime_config(settings: Settings, data: dict[str, Any]) -> None:
    local_path = Path(settings.local_settings_path)
    local_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mask_secret(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"
