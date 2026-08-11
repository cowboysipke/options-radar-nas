from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .parser import parse_llm_json


class OpenAIResponsesClient:
    """Minimal Responses API client with no mandatory third-party SDK."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", timeout: int = 90):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM HTTP {exc.code}: {body[:500]}") from exc

    @staticmethod
    def _output_text(response: Dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str):
            return str(response["output_text"])
        chunks = []
        for item in response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        return "".join(chunks)

    def refine_signal(self, text: str, base: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return base
        prompt = (
            "You normalize an unusual-options analyst card. Preserve stated facts only. "
            "Return JSON with keys rationale (array of concise strings) and risk_notes (array). "
            "Do not invent prices, direction, DTE, or Greeks.\n"
            f"Deterministic extraction: {json.dumps(base, ensure_ascii=False)}\nCard:\n{text}"
        )
        response = self._post({"model": self.model, "input": prompt})
        return parse_llm_json(self._output_text(response))

    def ocr_discord_image(self, path: Path) -> str:
        if not self.enabled:
            raise RuntimeError("LLM_API_KEY is empty")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Transcribe every visible Discord option-flow card exactly. Keep one card per paragraph. Ignore navigation UI."},
                    {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
                ],
            }],
        }
        return self._output_text(self._post(payload))
