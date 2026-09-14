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

from .logging_utils import get_logger

log = get_logger("mir_ai.azure_gateway")


def _cfg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EndpointConfig:
    endpoint: str
    api_key: str
    api_version: str
    deployment: str
    model: str
    url_template: str
    send_model: bool
    send_dimensions: bool


class AzureRequestError(requests.HTTPError):
    """HTTP failure retaining safe Azure/APIM diagnostics, never request payloads or keys."""

    def __init__(self, *, service: str, operation: str, response: requests.Response,
                 error_summary: str, request_id: str = "", retryable: bool = False):
        self.service = service
        self.operation = operation
        self.status_code = int(response.status_code)
        self.error_summary = error_summary
        self.request_id = request_id
        self.retryable = bool(retryable)
        super().__init__(
            f"{service} {operation} failed with HTTP {self.status_code}; "
            f"request_id={request_id or 'n/a'}; response={error_summary}",
            response=response,
        )


class AzureGateway:
    DEFAULT_URL_TEMPLATE = "{endpoint}/openai/deployments/{deployment}/{path}?api-version={api_version}"

    def __init__(self, config_path: str | Path = "config.ini", max_retries: int = 5,
                 timeout_s: int = 120, retry_base_s: float = 1.0, *,
                 embedding_model: str | None = None, embedding_dim: int = 3072,
                 embedding_api_version: str | None = None):
        self.config_path = Path(config_path)
        self.max_retries = max(1, int(max_retries))
        self.timeout_s = max(1, int(timeout_s))
        self.retry_base_s = max(0.0, float(retry_base_s))
        self.embedding_model = embedding_model
        self.embedding_dim = int(embedding_dim)
        self.embedding_api_version = embedding_api_version
        self._cfg = configparser.ConfigParser()
        self._cfg.read(self.config_path)
        self._tls = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._tls, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._tls.session = session
        return session

    def _section(self, aliases: tuple[str, ...], *, model_override: Optional[str] = None,
                 api_version_override: Optional[str] = None) -> EndpointConfig:
        section = next((s for s in aliases if self._cfg.has_section(s)), None)
        if not section:
            raise KeyError(f"None of the config sections exist: {aliases}")
        c = self._cfg[section]
        endpoint = c.get("endpoint", c.get("api_base", "")).rstrip("/")
        if not endpoint:
            raise KeyError(f"Missing endpoint/api_base in [{section}]")
        api_key = c.get("api_key", "").strip()
        if not api_key:
            raise KeyError(f"Missing api_key in [{section}]")
        model = (model_override or c.get("model", "")).strip()
        deployment = c.get("deployment", model).strip()
        if not deployment:
            raise KeyError(f"Missing deployment/model in [{section}]")
        if not model:
            model = deployment
        api_version = (api_version_override or c.get("api_version", "")).strip()
        if not api_version:
            raise KeyError(f"Missing api_version in [{section}]")
        url_template = c.get("url_template", self.DEFAULT_URL_TEMPLATE).strip()
        if not url_template:
            raise ValueError(f"url_template in [{section}] must not be empty")
        return EndpointConfig(endpoint, api_key, api_version, deployment, model, url_template,
                              _cfg_bool(c.get("send_model", "true"), True),
                              _cfg_bool(c.get("send_dimensions", "false"), False))

    @staticmethod
    def _url(config: EndpointConfig, path: str) -> str:
        try:
            return config.url_template.format(endpoint=config.endpoint, deployment=config.deployment,
                                              path=path, api_version=config.api_version, model=config.model)
        except KeyError as exc:
            raise ValueError(f"Unknown placeholder in Azure url_template: {exc}") from exc

    def _retry_delay(self, response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after:
                try:
                    return max(0.0, min(120.0, float(retry_after)))
                except ValueError:
                    pass
        return min(60.0, self.retry_base_s * float(2 ** (attempt - 1)))

    @staticmethod
    def _image_mime(image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
        if image_bytes.startswith(b"\xff\xd8\xff"): return "image/jpeg"
        if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP": return "image/webp"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
        raise ValueError("Unsupported visual image format for Azure multimodal description")

    @staticmethod
    def _request_id(response: requests.Response) -> str:
        for key in ("x-request-id", "apim-request-id", "x-ms-request-id", "request-id", "trace-id"):
            value = response.headers.get(key)
            if value:
                return str(value)[:200]
        return ""

    @staticmethod
    def _error_summary(response: requests.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            text = (response.text or "").replace("\r", " ").replace("\n", " ").strip()
            return text[:1500] or "<empty response body>"
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            selected = {}
            for key in ("code", "type", "param", "message", "details"):
                if key in error:
                    value = error[key]
                    if isinstance(value, str): value = value[:1500]
                    selected[key] = value
            if selected:
                return json.dumps(selected, ensure_ascii=False, default=str)[:3000]
        return json.dumps(error, ensure_ascii=False, default=str)[:3000]

    @staticmethod
    def _payload_metrics(json_body: dict[str, Any]) -> tuple[int, int]:
        inputs = json_body.get("input")
        item_count = len(inputs) if isinstance(inputs, list) else (1 if inputs is not None else 0)
        try: payload_chars = len(json.dumps(json_body, ensure_ascii=False, default=str))
        except Exception: payload_chars = 0
        return item_count, payload_chars

    def _post(self, url: str, api_key: str, *, json_body: dict[str, Any],
              service: str = "azure_openai", operation: str = "request") -> requests.Response:
        item_count, payload_chars = self._payload_metrics(json_body)
        retryable_statuses = {408, 409, 425, 429}
        last_transport: requests.RequestException | None = None
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                response = self._session().post(url, headers={"api-key": api_key, "Content-Type": "application/json"},
                                                json=json_body, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_transport = exc
                log.warning("External request transport failure", extra={
                    "service": service, "operation": operation, "attempt": attempt,
                    "max_attempts": self.max_retries, "error_class": type(exc).__name__,
                    "retryable": attempt < self.max_retries,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                if attempt == self.max_retries: raise
                time.sleep(self._retry_delay(None, attempt) + random.random())
                continue
            retryable = response.status_code in retryable_statuses or 500 <= response.status_code < 600
            if not response.ok:
                summary = self._error_summary(response)
                request_id = self._request_id(response)
                log.warning("External request failed", extra={
                    "service": service, "operation": operation, "attempt": attempt,
                    "max_attempts": self.max_retries, "status_code": response.status_code,
                    "request_id": request_id, "error_detail": summary,
                    "retryable": retryable and attempt < self.max_retries,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                if retryable and attempt < self.max_retries:
                    time.sleep(self._retry_delay(response, attempt) + random.random())
                    continue
                raise AzureRequestError(service=service, operation=operation, response=response,
                                        error_summary=f"{summary}; input_items={item_count}; payload_chars={payload_chars}",
                                        request_id=request_id, retryable=retryable)
            log.debug("External request succeeded", extra={
                "service": service, "operation": operation, "attempt": attempt,
                "status_code": response.status_code, "request_id": self._request_id(response),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            return response
        if last_transport: raise last_transport
        raise RuntimeError("Azure request exhausted retries without a response")

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        config = self._section(("AZURE_OPENAI_EMBEDDING", "azure_embedding"),
                               model_override=self.embedding_model,
                               api_version_override=self.embedding_api_version)
        payload: dict[str, Any] = {"input": texts}
        if config.send_model: payload["model"] = config.model
        if config.send_dimensions: payload["dimensions"] = self.embedding_dim
        response = self._post(self._url(config, "embeddings"), config.api_key, json_body=payload,
                              service="azure_embedding", operation="embeddings")
        data = response.json()
        items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(not isinstance(v, list) or len(v) != self.embedding_dim for v in vectors):
            raise ValueError(f"Embedding response shape/dimension mismatch; expected one {self.embedding_dim}-d vector per input")
        return vectors

    def preflight_embeddings(self) -> None:
        vectors = self.embeddings(["MIR-AI embedding connectivity preflight"])
        if len(vectors) != 1 or len(vectors[0]) != self.embedding_dim:
            raise RuntimeError("Embedding preflight returned an invalid vector shape")

    def preflight_chat(self) -> None:
        result = self.chat_text([
            {"role": "system", "content": "Return only the word OK."},
            {"role": "user", "content": "Connectivity preflight"},
        ], max_tokens=8, temperature=0.0)
        if not result.strip():
            raise RuntimeError("Chat preflight returned an empty response")

    @staticmethod
    def _response_format_unsupported(exc: AzureRequestError) -> bool:
        if exc.status_code != 400:
            return False
        text = exc.error_summary.casefold()
        return "response_format" in text and any(x in text for x in ("unsupported", "not supported", "unknown", "invalid"))

    def chat_json(self, messages: list[dict[str, Any]], *, max_tokens: int = 3000,
                  temperature: float = 0.0) -> dict[str, Any]:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        payload: dict[str, Any] = {"messages": messages, "temperature": temperature,
                                   "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
        if config.send_model: payload["model"] = config.model
        try:
            data = self._post(self._url(config, "chat/completions"), config.api_key, json_body=payload,
                              service="azure_chat", operation="chat_json").json()
        except AzureRequestError as exc:
            if not self._response_format_unsupported(exc):
                raise
            payload.pop("response_format", None)
            data = self._post(self._url(config, "chat/completions"), config.api_key, json_body=payload,
                              service="azure_chat", operation="chat_json_without_response_format").json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return json.loads(text)

    def _describe_visual(self, image_bytes: bytes, prompt: str, *, max_tokens: int = 800) -> str:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        mime = self._image_mime(image_bytes)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": "high"}},
            ]}], "temperature": 0.0, "max_tokens": max_tokens,
        }
        if config.send_model: payload["model"] = config.model
        data = self._post(self._url(config, "chat/completions"), config.api_key, json_body=payload,
                          service="azure_chat", operation="visual_description").json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list): return "".join(p.get("text", "") for p in text if isinstance(p, dict)).strip()
        return str(text).strip()

    def describe_chart(self, image_bytes: bytes, context: str = "") -> str:
        prompt = ("Extract only information visibly supported by this chart/graph. Report chart type, visible title, "
                  "axes/units, legend labels, explicit plotted values when readable, and clearly visible trends. "
                  "If something is not readable, say it is not readable; do not infer values. " +
                  (f"Nearby document text: {context[:1000]}" if context else ""))
        return self._describe_visual(image_bytes, prompt, max_tokens=800)

    def describe_image(self, image_bytes: bytes, context: str = "") -> str:
        prompt = ("Describe only content visibly supported by this document image for retrieval. Include visible subject "
                  "matter, labels, annotations, captions, symbols, and scientifically relevant relationships that can be "
                  "directly observed. Do not infer identity, measurements, diagnoses, values, or conclusions that are not "
                  "visibly supported. If decorative or not meaningful for retrieval, say so briefly. " +
                  (f"Nearby document text: {context[:1000]}" if context else ""))
        return self._describe_visual(image_bytes, prompt, max_tokens=600)

    def chat_text(self, messages: list[dict[str, Any]], *, max_tokens: int = 800,
                  temperature: float = 0.0) -> str:
        config = self._section(("AZURE_OPENAI_CHAT", "azure_chat"))
        payload: dict[str, Any] = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if config.send_model: payload["model"] = config.model
        data = self._post(self._url(config, "chat/completions"), config.api_key, json_body=payload,
                          service="azure_chat", operation="chat_text").json()
        text = data["choices"][0]["message"]["content"]
        if isinstance(text, list): return "".join(p.get("text", "") for p in text if isinstance(p, dict)).strip()
        return str(text).strip()
