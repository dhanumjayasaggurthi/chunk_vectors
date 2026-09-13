from __future__ import annotations

import configparser
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VisionResult:
    text: str
    confidence: float
    word_count: int
    language: str
    success: bool
    error: str = ""


class VisionOCR:
    def __init__(self, config_path: str | Path = "config.ini", max_retries: int = 5):
        self.config_path = Path(config_path)
        self.max_retries = max_retries
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        cfg = configparser.ConfigParser(); cfg.read(self.config_path)
        key_path = ""
        for section, key in (("GCP", "gcp_key_path"), ("PATHS", "gcp_credentials_path"), ("paths", "gcp_credentials_path")):
            if cfg.has_section(section) and cfg.has_option(section, key):
                key_path = cfg.get(section, key).strip(); break
        if key_path and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            p = Path(key_path)
            if not p.exists():
                raise FileNotFoundError(f"Configured GCP credentials file does not exist: {p}")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(p)
        from google.cloud import vision
        self._client = vision.ImageAnnotatorClient()
        return self._client

    def ocr(self, image_bytes: bytes) -> VisionResult:
        if not image_bytes:
            return VisionResult("", 0.0, 0, "", False, "empty_input")
        from google.cloud import vision
        image = vision.Image(content=image_bytes)
        context = vision.ImageContext(language_hints=["en", "de", "fr", "es", "it", "nl", "pt", "ja"])
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._get_client().document_text_detection(image=image, image_context=context)
                if response.error.message:
                    raise RuntimeError(response.error.message)
                full = response.full_text_annotation
                if not full or not full.text:
                    return VisionResult("", 0.0, 0, "", True)
                confidences = []; languages = []
                for page in full.pages:
                    for lang in page.property.detected_languages:
                        if lang.language_code: languages.append(lang.language_code)
                    for block in page.blocks:
                        for para in block.paragraphs:
                            for word in para.words: confidences.append(float(word.confidence or 0.0))
                confidence = sum(confidences) / len(confidences) if confidences else 0.0
                return VisionResult(full.text.strip(), confidence, len(confidences), languages[0] if languages else "", True, "low_confidence" if confidences and confidence < 0.60 else "")
            except Exception as exc:
                last_error = str(exc)
                if attempt == self.max_retries: break
                time.sleep(min(30.0, 2 ** (attempt - 1)) + random.random())
        return VisionResult("", 0.0, 0, "", False, last_error)
