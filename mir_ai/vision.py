from __future__ import annotations

import configparser
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .logging_utils import get_logger

log = get_logger("mir_ai.vision")


class VisionOCRFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool):
        self.retryable = bool(retryable)
        self.error_category = "GOOGLE_VISION_TRANSIENT" if retryable else "GOOGLE_VISION_ERROR"
        self.service = "google_vision"
        self.operation = "document_text_detection"
        super().__init__(json.dumps({"service": self.service, "operation": self.operation, "error_category": self.error_category, "retryable": self.retryable, "message": message}, ensure_ascii=False))


@dataclass
class VisionResult:
    text: str
    confidence: float
    word_count: int
    language: str
    success: bool
    error: str = ""
    retryable: bool = False


class VisionOCR:
    def __init__(self, config_path: str | Path = "config.ini", max_retries: int = 5,
                 timeout_s: int = 120, retry_base_s: float = 1.0, raise_on_failure: bool = False):
        self.config_path = Path(config_path)
        self.max_retries = max(1, int(max_retries))
        self.timeout_s = max(10, int(timeout_s))
        self.retry_base_s = max(0.0, float(retry_base_s))
        self.raise_on_failure = bool(raise_on_failure)
        self._client = None

    def _get_client(self):
        if self._client is not None: return self._client
        cfg=configparser.ConfigParser(); cfg.read(self.config_path)
        key_path=""
        for section,key in (("GCP","gcp_key_path"),("PATHS","gcp_credentials_path"),("paths","gcp_credentials_path")):
            if cfg.has_section(section) and cfg.has_option(section,key):
                key_path=cfg.get(section,key).strip(); break
        if key_path and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            p=Path(key_path)
            if not p.exists(): raise FileNotFoundError(f"Configured GCP credentials file does not exist: {p}")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"]=str(p)
        from google.cloud import vision
        self._client=vision.ImageAnnotatorClient()
        return self._client

    def preflight(self) -> None:
        # Client construction validates local credential/provider configuration without
        # sending document content. It does not prove the remote API will accept a later OCR call.
        self._get_client()

    @staticmethod
    def _retryable(exc: BaseException) -> bool:
        name=type(exc).__name__.casefold(); text=str(exc).casefold()
        return any(x in name for x in ("deadlineexceeded","serviceunavailable","toomanyrequests","internalservererror")) or any(
            x in text for x in ("deadline exceeded","temporarily unavailable","connection reset","connection aborted","429","503","504")
        )

    def ocr(self, image_bytes: bytes) -> VisionResult:
        if not image_bytes: return VisionResult("",0.0,0,"",False,"empty_input",False)
        from google.cloud import vision
        image=vision.Image(content=image_bytes)
        context=vision.ImageContext(language_hints=["en","de","fr","es","it","nl","pt","ja"])
        last_error=""; last_retryable=False
        for attempt in range(1,self.max_retries+1):
            started=time.monotonic()
            try:
                response=self._get_client().document_text_detection(image=image,image_context=context,timeout=self.timeout_s)
                if response.error.message: raise RuntimeError(response.error.message)
                full=response.full_text_annotation
                if not full or not full.text: return VisionResult("",0.0,0,"",True,"",False)
                confidences=[]; languages=[]
                for page in full.pages:
                    for lang in page.property.detected_languages:
                        if lang.language_code: languages.append(lang.language_code)
                    for block in page.blocks:
                        for para in block.paragraphs:
                            for word in para.words: confidences.append(float(word.confidence or 0.0))
                confidence=sum(confidences)/len(confidences) if confidences else 0.0
                return VisionResult(full.text.strip(),confidence,len(confidences),languages[0] if languages else "",True,
                                    "low_confidence" if confidences and confidence<0.60 else "",False)
            except Exception as exc:
                last_error=str(exc); retryable=self._retryable(exc); last_retryable=retryable
                log.warning("Google Vision OCR request failed",extra={
                    "service":"google_vision","operation":"document_text_detection","attempt":attempt,
                    "max_attempts":self.max_retries,"error_class":type(exc).__name__,
                    "error_detail":last_error[:1500],"retryable":retryable and attempt<self.max_retries,
                    "elapsed_ms":int((time.monotonic()-started)*1000),
                })
                if not retryable or attempt==self.max_retries: break
                time.sleep(min(30.0,self.retry_base_s*(2**(attempt-1)))+random.random())
        if self.raise_on_failure:
            raise VisionOCRFailure(last_error or "Google Vision OCR failed without a returned error message", retryable=last_retryable)
        return VisionResult("",0.0,0,"",False,last_error,last_retryable)
