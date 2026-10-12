"""eljazari.ai — LLM Service (Ollama wrapper)"""

import logging
from typing import Any

import httpx
from config.settings import settings

logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self, timeout_s: float = 120.0) -> None:
        self.base_url = settings.ollama_base_url
        self.client = httpx.AsyncClient(timeout=timeout_s)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.3,
        response_format: str | dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> str:
        model = model or settings.ollama_default_model
        payload = {
            "model": model,
            "prompt": prompt,
            "temperature": temperature,
            "stream": False,
        }
        if response_format is not None:
            payload["format"] = response_format
        if options:
            payload["options"] = options
        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=request_timeout,
        )
        response.raise_for_status()
        return response.json()["response"]

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(f"{self.base_url}/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        resp = await self.client.get(f"{self.base_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
