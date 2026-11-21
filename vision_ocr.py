"""
vision_ocr.py
=============
Google Cloud Vision API integration for the EPOD pipeline.

Handles two scenarios:
  1. Scanned pages    — entire page rendered as PNG → full-page OCR
  2. Image regions    — embedded image crops from pdf_processor → region OCR
  3. Chart regions    — Vision API + GPT-4o to describe chart content as text

Features:
  - Exponential back-off retry  (rate limits / transient errors)
  - Batch processing            (multiple images in one API call where possible)
  - Confidence filtering        (discard low-confidence OCR output)
  - Language hints              (English + common regulatory doc languages)
  - Reading order preservation  (Vision API returns word-level bounding polys)
  - Never raises                (returns empty string on failure, logs error)

Dependencies:
    pip install google-cloud-vision pillow
    GOOGLE_APPLICATION_CREDENTIALS set  OR  gcp_key_path in config.ini [GCP]
"""

from __future__ import annotations

import base64
import io
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import GCP_KEY_PATH, OCR_RENDER_DPI, API_MAX_RETRIES, API_RETRY_DELAY_S
from logger import get_logger

logger = get_logger("epod.vision_ocr")

# ── Lazy imports (so syntax check + tests work without packages) ─────────────
def _vision():
    from google.cloud import vision
    return vision

def _Image():
    from PIL import Image
    return Image
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# GCP client — initialised once per process
# ─────────────────────────────────────────────────────────────────────────────
_vision_client = None

def _get_client():
    """
    Return a cached Vision ImageAnnotatorClient.
    Sets GOOGLE_APPLICATION_CREDENTIALS from config.ini [GCP] if not already set.
    """
    global _vision_client
    if _vision_client is not None:
        return _vision_client

    # Set credentials path from config if env var not already set
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        key_path = Path(GCP_KEY_PATH)
        if key_path.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(key_path)
            logger.info(f"GCP credentials set from config: {key_path}")
        else:
            logger.warning(
                f"GCP key not found at {key_path}. "
                "Ensure GOOGLE_APPLICATION_CREDENTIALS env var is set."
            )

    vision = _vision()
    _vision_client = vision.ImageAnnotatorClient()
    logger.info("Google Vision client initialised.")
    return _vision_client


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OcrResult:
    """Result from OCR on a single image."""
    text:        str           # extracted + cleaned text
    confidence:  float         # 0.0 – 1.0  (mean word confidence)
    word_count:  int           # number of words detected
    language:    str           # detected language code e.g. "en"
    success:     bool          # False if API call failed
    error:       str = ""      # error message if success=False
    regions:     list[dict] = field(default_factory=list)  # OCR polygons and text


# ─────────────────────────────────────────────────────────────────────────────
# Core OCR function
# ─────────────────────────────────────────────────────────────────────────────

# Language hints for regulatory / pharmaceutical documents
_LANGUAGE_HINTS = ["en", "de", "fr", "es", "it", "nl", "pt", "ja"]

# Minimum mean word confidence to accept OCR output
_MIN_CONFIDENCE = 0.60


def ocr_image_bytes(
    image_bytes: bytes,
    language_hints: list[str] = None,
    min_confidence: float     = _MIN_CONFIDENCE,
) -> OcrResult:
    """
    Run Google Cloud Vision DOCUMENT_TEXT_DETECTION on raw image bytes (PNG/JPEG).

    DOCUMENT_TEXT_DETECTION is preferred over TEXT_DETECTION for multi-paragraph
    documents — it preserves layout structure and returns per-word confidence.

    Returns OcrResult — never raises.
    """
    if not image_bytes:
        return OcrResult(text="", confidence=0.0, word_count=0,
                         language="", success=False, error="empty_input")

    hints = language_hints or _LANGUAGE_HINTS
    vision = _vision()

    image = vision.Image(content=image_bytes)
    context = vision.ImageContext(language_hints=hints)

    last_error = ""
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            client = _get_client()
            response = client.document_text_detection(
                image=image,
                image_context=context,
            )

            # Check for API-level errors
            if response.error.message:
                raise RuntimeError(f"Vision API error: {response.error.message}")

            # Extract full text annotation
            full_text = response.full_text_annotation
            if not full_text or not full_text.text:
                return OcrResult(text="", confidence=0.0, word_count=0,
                                 language="", success=True)

            # Compute mean word confidence + collect text in reading order
            words_text  = []
            confidences = []
            lang_codes  = []

            for page in full_text.pages:
                # Detected language
                for prop in page.property.detected_languages:
                    if prop.language_code:
                        lang_codes.append(prop.language_code)

                for block in page.blocks:
                    for para in block.paragraphs:
                        para_words = []
                        for word in para.words:
                            conf = word.confidence if word.confidence else 0.0
                            confidences.append(conf)
                            word_text = "".join(
                                sym.text for sym in word.symbols
                            )
                            para_words.append(word_text)
                        words_text.append(" ".join(para_words))
                    words_text.append("\n")   # paragraph break

            mean_conf  = sum(confidences) / len(confidences) if confidences else 0.0
            raw_text   = "\n".join(words_text).strip()
            clean_text = _clean_ocr_text(raw_text)
            language   = lang_codes[0] if lang_codes else "en"

            # Discard very low-confidence output
            if mean_conf < min_confidence and len(confidences) > 5:
                logger.warning(
                    f"OCR confidence {mean_conf:.2f} below threshold "
                    f"{min_confidence} — discarding output"
                )
                return OcrResult(
                    text="", confidence=mean_conf,
                    word_count=len(confidences), language=language,
                    success=True,
                    error=f"low_confidence_{mean_conf:.2f}",
                )

            return OcrResult(
                text=clean_text,
                confidence=mean_conf,
                word_count=len(confidences),
                language=language,
                success=True,
            )

        except Exception as e:
            last_error = str(e)
            if attempt < API_MAX_RETRIES:
                delay = API_RETRY_DELAY_S * (2 ** (attempt - 1))   # exponential back-off
                logger.warning(
                    f"Vision OCR attempt {attempt}/{API_MAX_RETRIES} failed: {e} "
                    f"— retrying in {delay}s"
                )
                time.sleep(delay)
            else:
                logger.error(f"Vision OCR failed after {API_MAX_RETRIES} attempts: {e}")

    return OcrResult(
        text="", confidence=0.0, word_count=0,
        language="", success=False, error=last_error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in batched OCR for the staged large-document pipeline.
# Kept separate from ocr_image_bytes to avoid changes to production callers.
# ─────────────────────────────────────────────────────────────────────────────

def _polygon(vertices) -> list[list[int]]:
    """Preserve Vision's original quadrilateral, including rotated labels."""
    return [
        [int(getattr(v, "x", 0)), int(getattr(v, "y", 0))]
        for v in vertices
    ]


def _parse_batch_ocr(response, min_confidence: float, bbox_detail: str) -> OcrResult:
    if getattr(getattr(response, "error", None), "message", ""):
        return OcrResult(
            text="", confidence=0.0, word_count=0, language="",
            success=False, error=str(response.error.message)[:500],
        )
    full = getattr(response, "full_text_annotation", None)
    if not full or not getattr(full, "text", ""):
        return OcrResult(
            text="", confidence=0.0, word_count=0, language="",
            success=True,
        )
    regions, confidences, languages = [], [], []
    for page in getattr(full, "pages", []):
        for lang in getattr(getattr(page, "property", None), "detected_languages", []):
            if getattr(lang, "language_code", ""):
                languages.append(lang.language_code)
        for block in getattr(page, "blocks", []):
            blocks_words = []
            for para in getattr(block, "paragraphs", []):
                for word in getattr(para, "words", []):
                    text = "".join(getattr(sym, "text", "") for sym in word.symbols)
                    confidence = float(getattr(word, "confidence", 0.0) or 0.0)
                    confidences.append(confidence)
                    if text:
                        blocks_words.append(text)
                    if bbox_detail == "word":
                        regions.append({
                            "kind": "word", "text": text,
                            "confidence": round(confidence, 5),
                            "polygon": _polygon(word.bounding_box.vertices),
                        })
            if bbox_detail == "block":
                regions.append({
                    "kind": "block",
                    "text": " ".join(blocks_words),
                    "confidence": round(
                        sum(confidences[-len(blocks_words):]) / len(blocks_words),
                        5,
                    ) if blocks_words else 0.0,
                    "polygon": _polygon(block.bounding_box.vertices),
                })
    conf = sum(confidences) / len(confidences) if confidences else 0.0
    status = ""
    if confidences and conf < min_confidence:
        status = f"low_confidence_{conf:.3f}"
    return OcrResult(
        text=str(full.text).strip(),  # NO loss of numbers/short regulatory labels
        confidence=conf, word_count=len(confidences),
        language=languages[0] if languages else "",
        success=True, error=status, regions=regions,
    )


def batch_document_text_detection(
    image_bytes: list[bytes],
    *,
    limiter=None,
    max_retries: int = 4,
    timeout_seconds: int = 90,
    min_confidence: float = _MIN_CONFIDENCE,
    bbox_detail: str = "word",
) -> list[OcrResult]:
    """Send up to 16 images in one Vision annotate request.

    Returns results in EXACT input order. A failure is never converted to
    blank success, and retryable per-image failures retry only failed items.
    If the whole batch is invalid, bisect it to isolate corrupt inputs.
    The optional limiter accounts for BOTH request and image quotas.
    """
    import random
    if not 1 <= len(image_bytes) <= 16:
        raise ValueError("Vision synchronous batch must contain 1..16 images")
    if bbox_detail not in {"block", "word"}:
        raise ValueError("bbox_detail must be word or block")
    if not 1 <= max_retries <= 10 or not 5 <= timeout_seconds <= 600:
        raise ValueError("Invalid OCR retry / timeout settings")
    if any(not b or len(b) > 20 * 1024 * 1024 for b in image_bytes):
        raise ValueError("Image is empty or exceeds Vision's 20MB file limit")
    vision = _vision()
    client = _get_client()
    feature = vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)
    context = vision.ImageContext(language_hints=_LANGUAGE_HINTS)
    all_results: list[OcrResult | None] = [None] * len(image_bytes)

    def failure(message):
        return OcrResult(
            text="", confidence=0.0, word_count=0, language="",
            success=False, error=str(message)[:500],
        )

    def retryable(error):
        code = getattr(error, "code", None)
        if callable(code):
            try:
                code = code()
            except Exception:
                code = None
        code_str = str(getattr(code, "value", code)).upper()
        message = str(error).upper()
        return (
            any(k in code_str for k in ("429", "500", "503", "504", "8", "14"))
            or any(k in message for k in (
                "RESOURCE_EXHAUSTED", "UNAVAILABLE",
                "DEADLINE_EXCEEDED", "429", "503", "504",
            ))
        )

    def submit(indices, depth=0):
        waiting = list(indices)
        for attempt in range(max_retries):
            if not waiting:
                return
            requests = [
                vision.AnnotateImageRequest(
                    image=vision.Image(content=image_bytes[index]),
                    features=[feature], image_context=context,
                )
                for index in waiting
            ]
            try:
                if limiter:
                    limiter.acquire(len(waiting))
                answer = client.batch_annotate_images(
                    requests=requests, timeout=timeout_seconds, retry=None,
                )
                if len(answer.responses) != len(waiting):
                    raise RuntimeError("OCR_BATCH_RESPONSE_COUNT_MISMATCH")
                next_round = []
                for idx, response in zip(waiting, answer.responses):
                    item = _parse_batch_ocr(
                        response, min_confidence, bbox_detail,
                    )
                    if not item.success and retryable(getattr(response, "error", item.error)):
                        next_round.append(idx)
                    else:
                        all_results[idx] = item
                waiting = next_round
                if not waiting:
                    if limiter:
                        limiter.success()
                    return
                if limiter:
                    limiter.cooldown(10)
            except Exception as exc:
                message = str(exc)
                # Invalid single-image input is a per-image error. Never lose
                # successful neighbors by failing an entire healthy batch.
                if (
                    len(waiting) > 1 and depth < 5
                    and any(k in message.upper() for k in (
                        "INVALID_ARGUMENT", "400", "SIZE_LIMIT",
                    ))
                ):
                    middle = len(waiting) // 2
                    submit(waiting[:middle], depth + 1)
                    submit(waiting[middle:], depth + 1)
                    return
                if not retryable(exc):
                    for idx in waiting:
                        all_results[idx] = failure(
                            f"{type(exc).__name__}: {message}"
                        )
                    return
                if limiter:
                    limiter.cooldown(min(60, 5 * (2 ** attempt)))
            if attempt + 1 < max_retries:
                time.sleep(min(30, 1.5 * (2 ** attempt)) + random.random())
        for idx in waiting:
            all_results[idx] = failure("OCR_API_RETRIES_EXHAUSTED")

    submit(list(range(len(image_bytes))))
    # An unexpected missing response is not acceptable in an accuracy-first
    # ingestion pipeline, even when some siblings were processed correctly.
    return [
        item if item is not None else failure("OCR_RESPONSE_MISSING")
        for item in all_results
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Chart description  (Vision + GPT-4o)
# ─────────────────────────────────────────────────────────────────────────────

def describe_chart(image_bytes: bytes, context_text: str = "") -> OcrResult:
    """
    For chart/graph regions:
      1. Run Vision OCR to extract any embedded numbers/labels
      2. Send image to GPT-4o vision with a prompt to describe the chart
      3. Return combined text: OCR labels + GPT description

    Falls back to OCR-only if GPT-4o call fails.
    """
    if not image_bytes:
        return OcrResult(text="", confidence=0.0, word_count=0,
                         language="en", success=False, error="empty_input")

    # Step 1: OCR to get any text/numbers visible in the chart
    ocr_result = ocr_image_bytes(image_bytes, min_confidence=0.40)
    ocr_text   = ocr_result.text

    # Step 2: GPT-4o vision description
    gpt_description = _describe_chart_with_gpt4o(image_bytes, context_text, ocr_text)

    # Combine: OCR labels + structured description
    if gpt_description:
        combined = (
            f"[CHART/GRAPH DESCRIPTION]\n{gpt_description}"
            + (f"\n\n[CHART LABELS/VALUES]\n{ocr_text}" if ocr_text else "")
        )
    else:
        combined = f"[CHART CONTENT]\n{ocr_text}" if ocr_text else ""

    return OcrResult(
        text=combined,
        confidence=ocr_result.confidence,
        word_count=ocr_result.word_count,
        language=ocr_result.language,
        success=True,
    )


def _describe_chart_with_gpt4o(
    image_bytes: bytes,
    context_text: str,
    ocr_labels: str,
) -> str:
    """
    Call GPT-4o vision to describe a chart image as structured text.
    Returns description string, or "" on failure.
    """
    try:
        from azure_client import call_chat

        b64 = base64.b64encode(image_bytes).decode("utf-8")

        context_hint = ""
        if context_text:
            context_hint = f"Document context near this chart:\n{context_text[:500]}\n\n"
        if ocr_labels:
            context_hint += f"Text/labels detected in chart: {ocr_labels[:300]}\n\n"

        prompt = (
            f"{context_hint}"
            "Describe this chart or graph for a text retrieval system. Include:\n"
            "1. Chart type (bar, line, pie, scatter, table, etc.)\n"
            "2. Title if visible\n"
            "3. Axes labels and units\n"
            "4. Key data points, trends or conclusions\n"
            "5. Any legends or annotations\n"
            "Be concise and factual. Use plain text only — no markdown."
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        description, _ = call_chat(messages, temperature=0.1, max_tokens=400)
        return description.strip()

    except Exception as e:
        logger.warning(f"GPT-4o chart description failed: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Batch OCR  (process multiple images efficiently)
# ─────────────────────────────────────────────────────────────────────────────

def ocr_page_elements(
    elements: list,          # list of PageElement from pdf_processor
    doc_name: str = "",
    page_number: int = 0,
    *,
    raise_on_error: bool = False,
) -> list:
    """
    Run OCR/description on all IMAGE and CHART elements in a page's element list.
    Mutates element.text in-place. Returns the modified list.

    Processes IMAGE elements with ocr_image_bytes.
    Processes CHART elements with describe_chart (OCR + GPT-4o).
    Skips TEXT and TABLE elements (handled elsewhere).
    """
    from pdf_processor import ElementType   # local import to avoid circular

    img_count   = 0
    chart_count = 0

    for el in elements:
        if not el.image_bytes:
            continue

        # Gather nearby text context (elements already in reading order)
        context = " ".join(
            e.text for e in elements
            if e.text and e != el
        )[:600]

        if el.element_type == ElementType.CHART:
            result = describe_chart(el.image_bytes, context_text=context)
            chart_count += 1
        elif el.element_type == ElementType.IMAGE:
            result = ocr_image_bytes(el.image_bytes)
            img_count += 1
        else:
            continue

        el.text       = result.text
        el.confidence = result.confidence

        if result.error or not result.success:
            logger.warning(
                f"{doc_name} p{page_number}: OCR issue on "
                f"{el.element_type} — {result.error or 'no usable result'}"
            )
            if raise_on_error:
                raise RuntimeError(
                    f"OCR_UNRESOLVED page={page_number} "
                    f"element={el.element_type} "
                    f"reason={result.error or 'no usable result'}"
                )

    if img_count or chart_count:
        logger.debug(
            f"{doc_name} p{page_number}: OCR done — "
            f"{img_count} images, {chart_count} charts"
        )

    return elements


# ─────────────────────────────────────────────────────────────────────────────
# Image pre-processing helpers (improve OCR accuracy)
# ─────────────────────────────────────────────────────────────────────────────

def preprocess_for_ocr(image_bytes: bytes) -> bytes:
    """
    Apply basic image enhancements before sending to Vision API:
      - Convert to RGB (remove alpha)
      - Ensure minimum resolution (upscale if < 150 DPI equivalent)
      - Convert back to PNG bytes

    Returns original bytes on failure.
    """
    try:
        Image = _Image()
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Upscale very small images (often decorative / low-res scans)
        w, h = img.size
        if w < 600 or h < 600:
            scale = max(600 / w, 600 / h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    except Exception as e:
        logger.debug(f"Image pre-processing failed: {e} — using original")
        return image_bytes


def png_bytes_from_path(image_path: str) -> bytes:
    """Load any image file and return as PNG bytes."""
    try:
        Image = _Image()
        with Image.open(image_path) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
    except Exception as e:
        logger.error(f"Cannot load image {image_path}: {e}")
        return b""


# ─────────────────────────────────────────────────────────────────────────────
# Text cleaning
# ─────────────────────────────────────────────────────────────────────────────

def _clean_ocr_text(raw: str) -> str:
    """
    Clean raw Vision API output:
      - Strip control characters
      - Collapse excessive whitespace
      - Fix common OCR artifacts (l→1 only in numeric contexts)
      - Remove lines that are just noise (single chars, pure symbols)
    """
    import re

    if not raw:
        return ""

    lines = raw.split("\n")
    clean_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            clean_lines.append("")
            continue

        # Remove lines that are pure symbols / garbage
        # (less than 2 alphabetic characters in a line of 3+ chars)
        if len(line) >= 3:
            alpha_count = sum(1 for c in line if c.isalpha())
            if alpha_count < 2:
                continue

        # Remove control characters (keep \n, \t)
        line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", line)

        # Collapse multiple spaces
        line = re.sub(r" {2,}", " ", line)

        clean_lines.append(line)

    # Collapse 3+ blank lines to 2
    import re
    text = "\n".join(clean_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Test Vision OCR on an image file")
    ap.add_argument("image", help="Path to PNG/JPEG/PDF page image")
    ap.add_argument("--chart", action="store_true", help="Use chart description mode")
    args = ap.parse_args()

    img_bytes = png_bytes_from_path(args.image)
    if not img_bytes:
        print("Failed to load image.")
        exit(1)

    img_bytes = preprocess_for_ocr(img_bytes)

    if args.chart:
        result = describe_chart(img_bytes)
    else:
        result = ocr_image_bytes(img_bytes)

    print(f"\n{'='*60}")
    print(f"Success    : {result.success}")
    print(f"Confidence : {result.confidence:.3f}")
    print(f"Words      : {result.word_count}")
    print(f"Language   : {result.language}")
    print(f"{'='*60}")
    print(result.text)
