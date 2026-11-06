"""Calcolo degli embedding. Batch, ritentativi, troncamento di sicurezza."""
from __future__ import annotations

import logging
import time

from .config import settings

log = logging.getLogger(__name__)

# ~8191 token per text-embedding-3; taglio prudenziale in caratteri
MAX_CHARS = 24000


class Embedder:
    def __init__(self) -> None:
        from openai import OpenAI

        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY mancante: serve per il calcolo degli embedding."
            )
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.embed_model
        self.dim = settings.embed_dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = [t[:MAX_CHARS] if t.strip() else " " for t in texts]
        delay = 2.0
        for attempt in range(5):
            try:
                resp = self.client.embeddings.create(model=self.model, input=payload)
                return [d.embedding for d in resp.data]
            except Exception as exc:
                if attempt == 4:
                    raise
                log.warning("embedding fallito (tentativo %s): %s", attempt + 1, exc)
                time.sleep(delay)
                delay *= 2
        return []

    def encode_query(self, text: str) -> list[float]:
        return self.encode([text])[0]
