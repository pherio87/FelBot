"""Configurazione centrale. Tutto parametrizzabile da .env."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FLER_", extra="ignore", case_sensitive=False
    )

    # --- LLM ---
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 2000
    # temperatura 0: in ambito normativo la creativita' e' un difetto
    llm_temperature: float = 0.0

    embed_provider: Literal["openai"] = "openai"
    embed_model: str = "text-embedding-3-small"
    embed_dim: int = 1536
    embed_batch: int = 96

    # --- crawler ---
    user_agent: str = (
        "FelBot/1.2 (assistente documentale non ufficiale; +https://felbot.pherio.net/bot)"
    )
    # vuoto: nessuna intestazione From; il contatto e' la pagina indicata
    # nell'User-Agent
    contact_email: str = ""
    requests_per_second: float = 1.5
    max_concurrency: int = 3
    respect_robots: bool = True
    http_timeout: float = 60.0
    max_file_mb: int = 80

    # --- dati ---
    data_dir: Path = Path("./data")

    # --- retrieval ---
    top_k: int = 40
    context_chunks: int = 14
    min_relevance: float = 0.42
    chunk_target_chars: int = 1800
    chunk_overlap_chars: int = 250

    # --- ingestione ---
    # Il portale sostituisce allegati senza toccare la data di modifica: senza
    # una rivalidazione periodica un PDF cambiato non verrebbe mai ripreso.
    revalidate_days: int = 30

    # --- servizio ---
    access_password: str = ""
    require_password: bool = True
    cookie_secure: bool = True
    session_days: int = 14
    rate_login: str = "5/minute"
    rate_chat: str = "30/hour"
    host: str = "0.0.0.0"
    port: int = 8080

    # chiavi API lette senza prefisso FLER_
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    @property
    def lance_dir(self) -> Path:
        return self.data_dir / "lancedb"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.blobs_dir, self.lance_dir):
            p.mkdir(parents=True, exist_ok=True)


settings = Settings()  # type: ignore[call-arg]
