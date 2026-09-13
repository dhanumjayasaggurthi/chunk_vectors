from __future__ import annotations

import base64
import configparser
import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests


@dataclass(frozen=True)
class EndpointConfig:
    endpoint: str
    api_key: str
    api_version: str
    deployment: str
    model: str


class AzureGateway:
    def __init__(
        self,
        config_path: str | Path = "config.ini",
        max_retries: int = 5,
        timeout_s: int = 120,
    ):
        self.config_path = Path(config_path)
        self.max_retries = max(1, int(max_retries))
        self.timeout_s = timeout_s
        self._cfg = configparser.ConfigParser()
        self._cfg.read(self.config_path)
        self._tls = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._tls, "session", None)
        if session is None:
            session = requests.Session()
            self._tls.session = session
        return session

    def _section(
        self,
        aliases: tuple[str, ...],
        required_model: Optional[str] = None,
        required_api_version: Optional[str] = None,
    ) -> EndpointConfig:
        section = next((s for s in aliases if self._cfg.has_section(s)), None)
        if not section:
            raise KeyError(f"None of the config sections exist: {aliases}")
        config = self._cfg[section]
        endpoint = config.get("endpoint", config.get("api_base", "")).rstrip("/")
        if not endpoint:
            raise KeyError(f"Missing endpoint/api_base in [{section}]")
        api_key = config.get("api_key", "")
        if not api_key:
            raise KeyError(f"Missing api_key in [{section}]")
        model = config.get("model", required_model or "")
        deployment = config.get("deployment", model)
        if not deployment:
            raise KeyError(f"Missing deployment/model in [{section}]")
        api_version = required_api_version or config.get("api_version", "")
        if not api_version:
            raise KeyError(f"Missing api_version in [{section}]")
        return EndpointConfig(endpoint, api_key, api_version, deployment, model or deployment)

    @staticmethod
    def _url(config: EndpointConfig, path: str) -> str:
        return (
            f"{config.endpoint}/openai/deployments/{config.deployment}/{path}"
            f"?api-version={config.api_version}"
        )

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                return max(0.0, min(120.0, float(retry_after)))
            except ValueError:
                pass
        return min(60.0, float(2 ** (attempt - 1)))

    def _post(
        self,
        url: str,
        api_key: str,
        *,
        json_body: dict[str, Any],
    ) -> requests.Response:
        last_transport_error: requests.RequestException | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._session().post(
                    url,
                    headers={"api-key": api_key, "Content-Type": "application/json"},
                    json=json_body,
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                last_transport_error = exc
                if attempt == self.max_retries:
                    raise
                time.sleep(min(60.0, float(2 ** (attempt - 1))) + random.random())
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.max_retries:
                    response.raise_for_status()
                time.sleep(self._retry_delay(response, attempt) + random.random())
                continue

            response.raise_for_status()
            return response

        if last_transport_error is not None:
            raise last_transport_error
        raise RuntimeError("Azure request exhausted retries without a response")

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        config = self._section(
            ("AZURE_OPENAI_EMBEDDING", "azure_embedding"),
            required_model="text-embedding-3-large",
            required_api_version="2025-04-01-preview",
        )
        data = self._post(
            self._url(config, "embeddings"),
            config.api_key,
            json_body={"model": config.model, "input": texts},
        ).json()
        items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(
            not isinstance(vector, list) or len(vector) != 3072 for vector in vectors
        ):
            raise ValueError(
                "Embedding response shape/dimension mismatch; expected one 3072-d vector per input"
            )
        return vectors

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            data = self._post(
                self._url(config, "chat/completions"),
                config.api_key,
                json_body=payload,
            ).json()
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 400:
                raise
            payload.pop("response_format", None)
            data = self._post(
                self._url(config, "chat/completions"),
                config.api_key,
                json_body=payload,
            ).json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )
        return json.loads(text)

    def _describe_visual(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        max_tokens: int = 800,
    ) -> str:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        data = self._post(
            self._url(config, "chat/completions"),
            config.api_key,
            json_body=payload,
        ).json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            return "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            ).strip()
        return str(text).strip()

    def describe_chart(self, image_bytes: bytes, context: str = "") -> str:
        prompt = (
            "Extract only information visibly supported by this chart/graph. Report chart type, "
            "visible title, axes/units, legend labels, explicit plotted values when readable, and "
            "clearly visible trends. If something is not readable, say it is not readable; do not "
            "infer values. "
            + (f"Nearby document text: {context[:1000]}" if context else "")
        )
        return self._describe_visual(image_bytes, prompt, max_tokens=800)

    def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        prompt = (
            "Describe only content visibly supported by this document image for retrieval. "
            "Include visible subject matter, labels, annotations, captions, symbols, and "
            "scientifically relevant relationships that can be directly observed. Do not infer "
            "identity, measurements, diagnoses, values, or conclusions that are not visibly "
            "supported. If the image is decorative or contains no meaningful retrievable content, "
            "say so briefly. "
            + (f"Nearby document text: {context[:1000]}" if context else "")
        )
        return self._describe_visual(image_bytes, prompt, max_tokens=600)

    def chat_text(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 800,
        temperature: float = 0.0,
    ) -> str:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = self._post(
            self._url(config, "chat/completions"),
            config.api_key,
            json_body=payload,
        ).json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            return "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            ).strip()
        return str(text).strip()
