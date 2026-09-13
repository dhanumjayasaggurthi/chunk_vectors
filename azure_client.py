"""
azure_client_full.py
====================
Unified REST client for all JnJ Azure OpenAI endpoints.
Payload schemas match exactly what the JnJ API portal specifies per model.

  call_chat()         → AZURE_OPENAI_CHAT       gpt-4o
  call_gpt5()         → AZURE_OPENAI_GPT5        gpt-5 / gpt-5-mini / gpt-5-nano
  generate_image()    → AZURE_OPENAI_IMAGE        gpt-image-1-global
  edit_image()        → AZURE_OPENAI_IMAGE        gpt-image-1-global  (edits)
  text_to_speech()    → AZURE_OPENAI_AUDIO        gpt-4o-mini-tts-global
  transcribe_audio()  → AZURE_OPENAI_AUDIO        gpt-4o-transcribe-global
  get_embeddings()    → AZURE_OPENAI_EMBEDDING    text-embedding-3-small
  call_completion()   → AZURE_OPENAI_COMPLETION   gpt-35-turbo-instruct
"""

import configparser
import io
import requests
from typing import List, Dict, Tuple, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load(section: str) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read("config.ini")
    if section not in cfg:
        raise KeyError(
            f"[{section}] not found in config.ini.\n"
            f"Available sections: {cfg.sections()}"
        )
    return dict(cfg[section])


def _json_headers(api_key: str) -> dict:
    return {"api-key": api_key, "Content-Type": "application/json"}


def _url(api_base: str, deployment: str, path: str, api_version: str) -> str:
    return f"{api_base.rstrip('/')}/openai/deployments/{deployment}/{path}?api-version={api_version}"


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Chat Completion  —  GPT-4o  (text + multimodal / vision)
# ─────────────────────────────────────────────────────────────────────────────

def call_chat(
    messages: List[Dict],
    temperature: float = 0.3,
    max_tokens: int = 1200,
    section: str = "AZURE_OPENAI_CHAT",
) -> Tuple[str, dict]:
    """
    POST chat/completions.
    Messages can contain text-only or multimodal image_url parts.
    Returns (reply_text, usage_dict).
    """
    c = _load(section)
    payload = {
        "model":       c.get("model", c["deployment"]),
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    resp = requests.post(
        _url(c["api_base"], c["deployment"], "chat/completions", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"], data.get("usage", {})


# ─────────────────────────────────────────────────────────────────────────────
# 2.  GPT-5 Chat Completion
#
#  ⚠️  FIX: GPT-5 / o-series reasoning models do NOT accept:
#       temperature, top_p, frequency_penalty, presence_penalty
#     Only accepted params: model, messages, max_completion_tokens
#     Sending unsupported params causes a 400 Bad Request error.
#
#  NOTE: The signature keeps the old params so existing callers (ai_explain.py)
#        don't break — they are simply ignored and not sent to the API.
# ─────────────────────────────────────────────────────────────────────────────

def call_gpt5(
    messages: List[Dict],
    temperature: float = 1.0,           # kept for API compatibility, NOT sent to endpoint
    max_completion_tokens: int = 2000,
    top_p: float = 1.0,                 # kept for API compatibility, NOT sent to endpoint
    frequency_penalty: float = 0.0,     # kept for API compatibility, NOT sent to endpoint
    presence_penalty: float = 0.0,      # kept for API compatibility, NOT sent to endpoint
) -> Tuple[str, dict]:
    """
    POST to GPT-5 chat/completions endpoint.
    Uses max_completion_tokens (not max_tokens) as required by the portal spec.

    IMPORTANT: GPT-5 / o-series models reject temperature, top_p,
    frequency_penalty, and presence_penalty — those params are accepted
    in the function signature for backward compatibility but are intentionally
    excluded from the API payload to avoid a 400 Bad Request error.

    Returns (reply_text, usage_dict).
    """
    c = _load("AZURE_OPENAI_GPT5")

    # ✅ Only send what GPT-5 / o-series actually accepts
    payload = {
        "model":                 c.get("model", c["deployment"]),
        "messages":              messages,
        "max_completion_tokens": max_completion_tokens,
    }

    resp = requests.post(
        _url(c["api_base"], c["deployment"], "chat/completions", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=180,
    )

    # Log response body on failure to help with future debugging
    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} Error from GPT-5 endpoint.\n"
            f"URL: {resp.url}\n"
            f"Response: {err_body}",
            response=resp,
        )

    data = resp.json()
    return data["choices"][0]["message"]["content"], data.get("usage", {})


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Image Generation  —  gpt-image-1-global
#
#  ⚠️  gpt-image-1 does NOT accept:  style, response_format
#     Instead it uses:               output_format, output_compression
#  quality values: low | medium | high | auto   (NOT "standard" or "hd")
# ─────────────────────────────────────────────────────────────────────────────

def generate_image(
    prompt: str,
    n: int = 1,
    size: str = "1024x1024",           # 1024x1024 | 1024x1792 | 1792x1024
    quality: str = "low",              # low | medium | high | auto
    output_format: str = "png",        # png | jpeg | webp
    output_compression: int = 100,     # 0-100, only applies to jpeg/webp
) -> List[str]:
    """
    POST images/generations using gpt-image-1-global.
    Returns list of base-64 encoded image strings (b64_json).

    Note: gpt-image-1 always returns b64_json, not URLs.
    Decode with: base64.b64decode(result[0])
    """
    c = _load("AZURE_OPENAI_IMAGE")
    payload = {
        "model":              c.get("model", c["deployment"]),
        "prompt":             prompt,
        "n":                  n,
        "size":               size,
        "quality":            quality,
        "output_format":      output_format,
        "output_compression": output_compression,
    }
    resp = requests.post(
        _url(c["api_base"], c["deployment"], "images/generations", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    # gpt-image-1 returns b64_json
    results = []
    for item in data["data"]:
        if "b64_json" in item:
            results.append(item["b64_json"])
        elif "url" in item:
            results.append(item["url"])
    return results


def edit_image(
    image_bytes: bytes,
    prompt: str,
    mask_bytes: Optional[bytes] = None,
    n: int = 1,
    size: str = "1024x1024",
    quality: str = "low",
    output_format: str = "png",
) -> List[str]:
    """
    POST images/edits (inpainting) with gpt-image-1-global.
    image_bytes and mask_bytes should be PNG (RGBA for mask transparency).
    Returns list of b64_json strings.
    """
    c = _load("AZURE_OPENAI_IMAGE")
    headers = {"api-key": c["api_key"]}
    files: dict = {
        "image":         ("image.png", io.BytesIO(image_bytes), "image/png"),
        "prompt":        (None, prompt),
        "n":             (None, str(n)),
        "size":          (None, size),
        "quality":       (None, quality),
        "output_format": (None, output_format),
        "model":         (None, c.get("model", c["deployment"])),
    }
    if mask_bytes:
        files["mask"] = ("mask.png", io.BytesIO(mask_bytes), "image/png")

    resp = requests.post(
        _url(c["api_base"], c["deployment"], "images/edits", c["api_version"]),
        headers=headers,
        files=files,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return [item.get("b64_json", item.get("url", "")) for item in data["data"]]


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Text-to-Speech  —  gpt-4o-mini-tts-global
#     api_base: /openai-audio  (NOT /openai-chat)
#     api_version: 2025-03-01-preview
# ─────────────────────────────────────────────────────────────────────────────

def text_to_speech(
    text: str,
    voice: str = "alloy",         # alloy | echo | fable | onyx | nova | shimmer
    speed: float = 1.0,
    response_format: str = "mp3", # mp3 | opus | aac | flac
) -> bytes:
    """
    POST audio/speech to gpt-4o-mini-tts-global.
    Returns raw audio bytes (mp3 by default).
    Max ~4096 chars per call — chunk longer text before calling.
    """
    c = _load("AZURE_OPENAI_AUDIO")
    deployment = c.get("tts_deployment", "gpt-4o-mini-tts-global")
    model      = c.get("tts_model",      "gpt-4o-mini-tts")
    payload = {
        "model":           model,
        "input":           text,
        "voice":           voice,
        "response_format": response_format,
    }
    # speed only if not default to avoid rejection on some API versions
    if speed != 1.0:
        payload["speed"] = speed

    resp = requests.post(
        _url(c["api_base"], deployment, "audio/speech", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Transcription  —  gpt-4o-transcribe-global  (or mini variant)
#     api_base: /openai-audio  (NOT /openai-chat)
# ─────────────────────────────────────────────────────────────────────────────

def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    language: str = "en",
    prompt: str = "",
) -> str:
    """
    POST audio/transcriptions to gpt-4o-transcribe-global.
    Returns transcribed text string.
    Supports: wav, mp3, m4a, ogg, flac, webm
    """
    c = _load("AZURE_OPENAI_AUDIO")
    deployment = c.get("transcription_deployment", "gpt-4o-transcribe-global")
    model      = c.get("transcription_model",      "gpt-4o-transcribe")
    headers    = {"api-key": c["api_key"]}

    # Detect mime type from filename
    ext = filename.rsplit(".", 1)[-1].lower()
    mime_map = {
        "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4",
        "ogg": "audio/ogg", "flac": "audio/flac", "webm": "audio/webm",
    }
    mime = mime_map.get(ext, "audio/wav")

    files: dict = {
        "file":     (filename, io.BytesIO(audio_bytes), mime),
        "model":    (None, model),
        "language": (None, language),
    }
    if prompt:
        files["prompt"] = (None, prompt)

    resp = requests.post(
        _url(c["api_base"], deployment, "audio/transcriptions", c["api_version"]),
        headers=headers,
        files=files,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("text", "")


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Embeddings  —  text-embedding-3-small
#     Section: AZURE_OPENAI_EMBEDDING  (singular)
# ─────────────────────────────────────────────────────────────────────────────

def get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    POST embeddings for a list of strings.
    Returns list of float vectors (1536-dim for text-embedding-3-small).
    """
    c = _load("AZURE_OPENAI_EMBEDDING")
    payload = {
        "model": c.get("model", c["deployment"]),
        "input": texts,
    }
    resp = requests.post(
        _url(c["api_base"], c["deployment"], "embeddings", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return [item["embedding"] for item in resp.json()["data"]]


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Legacy Completion  —  gpt-35-turbo-instruct
# ─────────────────────────────────────────────────────────────────────────────

def call_completion(
    prompt: str,
    max_tokens: int = 800,
    temperature: float = 0.3,
) -> Tuple[str, dict]:
    """
    POST completions (legacy instruct-style, no messages list).
    Returns (text, usage_dict).
    """
    c = _load("AZURE_OPENAI_COMPLETION")
    payload = {
        "model":       c.get("model", c["deployment"]),
        "prompt":      prompt,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    resp = requests.post(
        _url(c["api_base"], c["deployment"], "completions", c["api_version"]),
        headers=_json_headers(c["api_key"]),
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["text"], data.get("usage", {})