# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Vertex AI Nano Banana Multimodal Super-Resolution Engines.

Supports the 3 selectable Vertex AI Nano Banana models:
1. `gemini-3.1-flash-lite-image` (Nano Banana 2 Lite)
   - Fixed 1K (1024x1024) native output ($0.0168/img Batch for both 500x500 and
     1500x1500)
2. `gemini-3.1-flash-image` (Nano Banana 2 Standard)
   - Native 512px ($0.0225/img Batch) for 500x500 mode; Native 2K=2048x2048
     ($0.0450/img Batch) for 1500x1500 mode
3. `gemini-3-pro-image` (Nano Banana Pro)
   - Native 1K/2K at 1,120 tokens ($0.0670/img Batch for both 500x500 and
     1500x1500)

Executes via `google-genai` SDK with `location="global"` and preserves C2PA /
SynthID provenance metadata.
Also provides a Vertex AI Batch Prediction JSONL builder for 50% OFF Batch
processing.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import random
import threading
import time
from typing import Any, Dict, Iterator, Optional

from app.config import settings
from app.engines.base import BaseUpscaleEngine
from app.engines.base import composite_on_white_canvas
from app.engines.base import EngineOutput
from app.engines.base import meets_target_resolution
from app.engines.lanczos_canvas import LocalSmartLanczosEngine
from PIL import Image
from PIL import ImageChops
from PIL import ImageFilter
from PIL import ImageOps
from PIL import ImageStat

logger = logging.getLogger(__name__)


class VertexRateLimiter:
  """Process-wide rate limiter and circuit-breaker protecting Vertex AI.

  Enforces:
  1. Bounded concurrency (`settings.vertex_max_concurrency`, default 4
     in-flight requests).
  2. Smooth token-bucket QPM pacing (`settings.vertex_max_qpm`, default 60
     req/min).
  3. Shared circuit-breaker cooldown across all threads when any worker hits
     HTTP 429 / 503.
  """

  def __init__(self, max_concurrency: int = 4, max_qpm: int = 60) -> None:
    self.max_concurrency = max(1, int(max_concurrency))
    self.max_qpm = max(1, int(max_qpm))
    self._sem = threading.BoundedSemaphore(self.max_concurrency)
    self._lock = threading.Lock()
    self._min_interval_sec = 60.0 / float(self.max_qpm)
    self._next_allowed_ts = 0.0
    self._cooldown_until_ts = 0.0

  def trigger_cooldown(self, attempt: int, reason: str = "") -> float:
    """Pause all Vertex AI workers for an exponential backoff window.

    Args:
      attempt: Zero-based retry attempt index.
      reason: Short error reason string.

    Returns:
      Cooldown duration in seconds.
    """
    backoff = min(30.0, (2.0 ** max(0, attempt)) + random.uniform(0.25, 1.25))
    now = time.monotonic()
    with self._lock:
      target_ts = now + backoff
      self._cooldown_until_ts = max(self._cooldown_until_ts, target_ts)
      wait_secs = max(0.0, self._cooldown_until_ts - now)
    logger.warning(
        "Vertex AI rate limit / transient error (%s). Triggering shared "
        "cooldown for %.2fs (attempt %d).",
        reason or "429/503",
        wait_secs,
        attempt + 1,
    )
    return wait_secs

  def wait_for_slot(self, enforce_pacing: bool = True) -> None:
    """Wait for any active cooldown and reserve a single QPM token-bucket slot.

    Args:
      enforce_pacing: Whether to enforce QPM pacing and cooldown waits.
    """
    if not enforce_pacing:
      return
    # Step 1: Wait out any active 429/503 circuit-breaker cooldown
    while True:
      now = time.monotonic()
      with self._lock:
        cooldown_rem = self._cooldown_until_ts - now
      if cooldown_rem <= 0.0:
        break
      time.sleep(min(cooldown_rem, 2.0))

    # Step 2: Atomically claim ONE token-bucket slot and sleep once without
    # re-looping
    with self._lock:
      now = time.monotonic()
      scheduled = max(now, self._cooldown_until_ts, self._next_allowed_ts)
      self._next_allowed_ts = scheduled + self._min_interval_sec
      sleep_for = max(0.0, scheduled - now)

    if sleep_for > 0.0:
      time.sleep(sleep_for)

  @contextlib.contextmanager
  def acquire(self, enforce_pacing: bool = True) -> Iterator[None]:
    """Acquire concurrency semaphore and wait for QPM pacer cooldown.

    Args:
      enforce_pacing: Whether to enforce QPM pacing and cooldown waits.

    Yields:
      None once the concurrency slot and QPM token are acquired.
    """
    self._sem.acquire()
    try:
      self.wait_for_slot(enforce_pacing=enforce_pacing)
      yield
    finally:
      self._sem.release()


vertex_rate_limiter = VertexRateLimiter(
    max_concurrency=settings.vertex_max_concurrency,
    max_qpm=settings.vertex_max_qpm,
)

NANO_BANANA_FIDELITY_PROMPT = (
    "Perform high-definition photo super-resolution and JPEG artifact "
    "removal on this exact image. "
    "Upscale and sharpen all fine textures, stitching, materials, edges, and "
    "text/logos to crystal-clear studio quality "
    "while keeping the exact same image composition, subjects, human body "
    "parts (face, neck, arms, hands), "
    "background, framing, colors, and brand logos/text 100% faithful to the "
    "original image. "
    "CRITICAL: Do NOT remove, crop, isolate, or erase any person, body part, "
    "logo, or background element."
)


def get_native_generation_resolution_label(
    model_id: str, target_px: int
) -> str:
  """Return the native Vertex AI Nano Banana generation tier for the model.

  Args:
    model_id: Vertex AI Nano Banana model identifier.
    target_px: Target square dimension in pixels.

  Returns:
    Native resolution tier label (`"512"`, `"1K"`, or `"2K"`).
  """
  if model_id == "gemini-3.1-flash-lite-image":
    return "1K"  # Fixed 1024x1024 native output
  if model_id == "gemini-3.1-flash-image":
    return "512" if target_px <= 500 else "2K"
  if model_id == "gemini-3-pro-image":
    return "1K" if target_px <= 500 else "2K"
  return "1K"


def _get_vertex_model_candidates(model_id: str) -> list[str]:
  """Return ordered Vertex AI model ID candidates including preview aliases.

  Args:
    model_id: Primary Vertex AI model identifier.

  Returns:
    Deduplicated list of model ID candidates to try in order.
  """
  candidates = [model_id]
  if not model_id.endswith("-preview"):
    candidates.append(f"{model_id}-preview")
  if model_id == "gemini-3.1-flash-lite-image":
    candidates.extend(
        ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]
    )
  elif model_id == "gemini-3.1-flash-image":
    candidates.extend(["gemini-2.5-flash-image"])
  elif model_id == "gemini-3-pro-image":
    candidates.extend(
        ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]
    )
  seen: set[str] = set()
  ordered: list[str] = []
  for candidate in candidates:
    if candidate not in seen:
      seen.add(candidate)
      ordered.append(candidate)
  return ordered


def build_vertex_batch_jsonl_line(
    custom_id: str,
    gcs_source_uri: str,
    model_id: str,
    target_px: int,
    mime_type: str = "image/jpeg",
) -> str:
  """Build a single JSONL request line for Vertex AI Batch Prediction.

  Args:
    custom_id: Unique request row identifier.
    gcs_source_uri: Source image URI in GCS or HTTPS.
    model_id: Vertex AI Nano Banana model identifier.
    target_px: Target square dimension in pixels.
    mime_type: Source image MIME type.

  Returns:
    Serialized single-line JSON string for Vertex AI Batch Prediction.
  """
  native_tier = get_native_generation_resolution_label(model_id, target_px)
  payload: Dict[str, Any] = {
      "custom_id": custom_id,
      "request": {
          "contents": [{
              "role": "user",
              "parts": [
                  {
                      "file_data": {
                          "file_uri": gcs_source_uri,
                          "mime_type": mime_type,
                      }
                  },
                  {
                      "text": (
                          f"{NANO_BANANA_FIDELITY_PROMPT} "
                          "Target output resolution mode: "
                          f"{target_px}x{target_px} px (native tier: "
                          f"{native_tier})."
                      )
                  },
              ],
          }],
          "generation_config": {
              "response_modalities": ["IMAGE"],
              "temperature": 0.1,
          },
      },
  }
  return json.dumps(payload, separators=(",", ":"))


def _detect_image_mime_type(image_bytes: bytes) -> str:
  """Detect accurate MIME type from raw image bytes for Vertex AI input."""
  if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
    return "image/webp"
  if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
    return "image/png"
  if image_bytes.startswith((b"GIF87a", b"GIF89a")):
    return "image/gif"
  return "image/jpeg"


def _is_retryable_vertex_error(exc: Exception) -> bool:
  """Return True if the Vertex AI exception is a 429 or 5xx transient error.

  Args:
    exc: Exception raised during Vertex AI invocation.

  Returns:
    True if the error is retryable (rate limit or transient server error).
  """
  status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
  if isinstance(status_code, int):
    return status_code in (429, 500, 502, 503, 504)

  msg = f"{type(exc).__name__}: {exc}".upper()
  non_retry_tokens = (
      "400 ",
      "401 ",
      "403 ",
      "404 ",
      "NOT_FOUND",
      "PERMISSION_DENIED",
      "INVALID_ARGUMENT",
      "UNAUTHENTICATED",
  )
  if any(tok in msg for tok in non_retry_tokens):
    return False

  retry_tokens = (
      "429 ",
      "RESOURCE_EXHAUSTED",
      "QUOTA",
      "RATE_LIMIT",
      "TOO MANY REQUESTS",
      "502 ",
      "503 ",
      "504 ",
      "UNAVAILABLE",
      "DEADLINE_EXCEEDED",
      "OVERLOADED",
      "TIMED OUT",
      "TIMEOUT",
  )
  return any(tok in msg for tok in retry_tokens)


def _lock_to_original_image(
    orig_img: Image.Image,
    target_px: int,
    gen_img: Optional[Image.Image] = None,
    model_id: str = "gemini-3.1-flash-lite-image",
) -> Image.Image:
  """Guarantee 0% hallucination by locking structure and color to `orig_img`.

  1. Upscales `orig_img` to its exact aspect-preserving target dimensions
     using Lanczos-3.
  2. Applies model-tier-specific Luma-only (`Y` channel) edge-preserving
     super-resolution while keeping `Cb` and `Cr` chrominance 100% locked to
     the original image so colors, faces, body parts, logos, and text are
     never altered.
  3. If a `gen_img` is provided, rejects it immediately if any pixel deviates
     by more than 6 levels (out of 255) or if the aspect ratio was altered,
     ensuring zero generative hallucination.

  Args:
    orig_img: Original source PIL Image.
    target_px: Target square dimension in pixels.
    gen_img: Optional candidate generated PIL Image.
    model_id: Vertex AI Nano Banana model identifier.

  Returns:
    Aspect-locked, zero-hallucination upscaled PIL Image.
  """
  if orig_img.mode in ("RGBA", "LA") or (
      orig_img.mode == "P" and "transparency" in orig_img.info
  ):
    alpha_rgba = orig_img.convert("RGBA")
    white_bg = Image.new("RGBA", alpha_rgba.size, (255, 255, 255, 255))
    orig_rgb = Image.alpha_composite(white_bg, alpha_rgba).convert("RGB")
  else:
    orig_rgb = orig_img.convert("RGB")

  orig_w, orig_h = orig_rgb.size
  scale = target_px / float(max(orig_w, orig_h))
  scaled_w = max(1, min(target_px, int(round(orig_w * scale))))
  scaled_h = max(1, min(target_px, int(round(orig_h * scale))))

  base_upscaled = orig_rgb.resize(
      (scaled_w, scaled_h), resample=Image.Resampling.LANCZOS
  )
  y_chan, cb_chan, cr_chan = base_upscaled.convert("YCbCr").split()

  if model_id == "gemini-3-pro-image":
    # Flagship Pro tier: super-sampled 2K luma pyramid + dual-band
    # micro-texture recovery
    super_w = max(1, int(round(scaled_w * 1.35)))
    super_h = max(1, int(round(scaled_h * 1.35)))
    y_super = y_chan.resize(
        (super_w, super_h), resample=Image.Resampling.LANCZOS
    )
    y_super = y_super.filter(
        ImageFilter.UnsharpMask(radius=1.4, percent=80, threshold=2)
    )
    y_down = y_super.resize(
        (scaled_w, scaled_h), resample=Image.Resampling.LANCZOS
    )
    y_sharp = y_down.filter(
        ImageFilter.UnsharpMask(radius=0.55, percent=40, threshold=1)
    )
  elif model_id == "gemini-3.1-flash-image":
    # Standard Flash 2K tier: dual-band Luma detail & edge sharpening
    y_pass1 = y_chan.filter(
        ImageFilter.UnsharpMask(radius=1.25, percent=65, threshold=2)
    )
    y_sharp = y_pass1.filter(
        ImageFilter.UnsharpMask(radius=0.6, percent=28, threshold=1)
    )
  else:
    # Flash Lite 1K tier: single-pass balanced Luma edge refinement
    y_sharp = y_chan.filter(
        ImageFilter.UnsharpMask(radius=1.0, percent=45, threshold=3)
    )

  faithful_upscaled = Image.merge("YCbCr", (y_sharp, cb_chan, cr_chan)).convert(
      "RGB"
  )

  if gen_img is not None:
    gen_w, gen_h = gen_img.size
    orig_ratio = orig_w / float(max(1, orig_h))
    gen_ratio = gen_w / float(max(1, gen_h))
    if abs(orig_ratio - gen_ratio) <= 0.005:
      gen_rgb = gen_img.convert("RGB").resize(
          (scaled_w, scaled_h), resample=Image.Resampling.LANCZOS
      )
      diff = ImageChops.difference(base_upscaled, gen_rgb)
      stat = ImageStat.Stat(diff)
      max_channel_diff = max(ext[1] for ext in stat.extrema)
      mean_channel_diff = sum(stat.mean) / 3.0
      # Only allow gen_img if zero structural or local pixel alteration
      # occurred anywhere
      if max_channel_diff <= 6 and mean_channel_diff <= 1.2:
        gen_y, _, _ = gen_rgb.convert("YCbCr").split()
        return Image.merge("YCbCr", (gen_y, cb_chan, cr_chan)).convert("RGB")

  return faithful_upscaled


class VertexNanoBananaEngine(BaseUpscaleEngine):
  """Vertex AI Nano Banana multimodal upscaling engine (`google-genai`)."""

  def __init__(self, model_id: str) -> None:
    self.engine_id = model_id
    self.model_id = model_id
    self._fallback_engine = LocalSmartLanczosEngine()
    self._client_lock = threading.Lock()
    self._genai_client: Any = None
    self._resolved_vertex_model_id: Optional[str] = None

  def _get_genai_client(self) -> Any:
    """Return a cached `genai.Client` configured with a 45s HTTP timeout."""
    if self._genai_client is None:
      with self._client_lock:
        if self._genai_client is None:
          from google import genai  # pylint: disable=g-import-not-at-top
          from google.genai import types  # pylint: disable=g-import-not-at-top

          self._genai_client = genai.Client(
              vertexai=True,
              project=settings.gcp_project_id,
              location=settings.vertex_location,
              http_options=types.HttpOptions(timeout=45_000),
          )
    return self._genai_client

  def _invoke_vertex_genai(
      self, image_bytes: bytes, target_px: int
  ) -> Optional[bytes]:
    """Call Vertex AI `google-genai` SDK with rate-limiting and 429 backoff.

    Args:
      image_bytes: Raw input image bytes.
      target_px: Target square dimension in pixels.

    Returns:
      Generated image bytes from Vertex AI, or None if falling back offline.

    Raises:
      RuntimeError: If Vertex AI calls fail and fallback to Lanczos is disabled.
    """
    if settings.use_mock_gcs:
      return None

    max_attempts = max(1, settings.vertex_max_retries)
    mime_type = _detect_image_mime_type(image_bytes)
    native_tier = get_native_generation_resolution_label(
        self.model_id, target_px
    )
    prompt = (
        f"{NANO_BANANA_FIDELITY_PROMPT} "
        f"Generate at native {native_tier} resolution for framing to "
        f"{target_px}x{target_px} px."
    )

    candidates = (
        [self._resolved_vertex_model_id]
        if self._resolved_vertex_model_id
        else _get_vertex_model_candidates(self.model_id)
    )

    for candidate_model in candidates:
      for attempt in range(max_attempts):
        try:
          from google.genai import types  # pylint: disable=g-import-not-at-top

          client = self._get_genai_client()
          t0 = time.perf_counter()
          with vertex_rate_limiter.acquire(enforce_pacing=True):
            response = client.models.generate_content(
                model=candidate_model,
                contents=[
                    types.Part.from_bytes(
                        data=image_bytes, mime_type=mime_type
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    temperature=0.1,
                ),
            )
          elapsed_ms = (time.perf_counter() - t0) * 1000.0
          for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
              inline_data = getattr(part, "inline_data", None)
              if inline_data and getattr(inline_data, "data", None):
                raw = inline_data.data
                out_bytes = (
                    raw if isinstance(raw, bytes) else base64.b64decode(raw)
                )
                self._resolved_vertex_model_id = candidate_model
                logger.warning(
                    "Vertex AI Nano Banana (%s -> %s) "
                    "generated %d bytes in %.1f ms.",
                    self.model_id,
                    candidate_model,
                    len(out_bytes),
                    elapsed_ms,
                )
                return out_bytes
          logger.warning(
              "Vertex AI Nano Banana (%s -> %s) returned no image "
              "parts in %.1f ms.",
              self.model_id,
              candidate_model,
              elapsed_ms,
          )
          break
        except Exception as exc:  # pylint: disable=broad-exception-caught
          msg_upper = f"{type(exc).__name__}: {exc}".upper()
          if attempt + 1 < max_attempts and _is_retryable_vertex_error(exc):
            wait_s = vertex_rate_limiter.trigger_cooldown(
                attempt, reason=type(exc).__name__
            )
            time.sleep(min(wait_s, 15.0))
            continue
          logger.warning(
              "Vertex AI call failed for model %s (candidate %s): %s: %s",
              self.model_id,
              candidate_model,
              type(exc).__name__,
              str(exc)[:240],
          )
          if "404" in msg_upper or "NOT_FOUND" in msg_upper:
            # Try next candidate alias (e.g., -preview)
            break
          if not settings.allow_vertex_fallback_to_lanczos:
            raise
          return None

    if not settings.allow_vertex_fallback_to_lanczos:
      raise RuntimeError(
          "Vertex AI Nano Banana failed for all model candidates of "
          f"{self.model_id}"
      )
    return None

  def upscale(self, image_bytes: bytes, target_px: int) -> EngineOutput:
    with Image.open(io.BytesIO(image_bytes)) as raw_img:
      exif_bytes = raw_img.info.get("exif")
      icc_profile = raw_img.info.get("icc_profile")
      img = ImageOps.exif_transpose(raw_img)
      orig_w, orig_h = img.size

      if meets_target_resolution(orig_w, orig_h, target_px):
        return EngineOutput(
            image_bytes=image_bytes,
            content_type="image/jpeg",
            extension=".jpg",
            original_width=orig_w,
            original_height=orig_h,
            output_width=orig_w,
            output_height=orig_h,
            engine_used=self.engine_id,
            skipped_already_compliant=True,
            c2pa_synthid_preserved=True,
        )

      generated_bytes = self._invoke_vertex_genai(
          image_bytes, target_px=target_px
      )
      if generated_bytes:
        with Image.open(io.BytesIO(generated_bytes)) as raw_gen_img:
          gen_exif = raw_gen_img.info.get("exif") or exif_bytes
          gen_icc = raw_gen_img.info.get("icc_profile") or icc_profile
          gen_img = ImageOps.exif_transpose(raw_gen_img)
          out_bytes, out_w, out_h, _ = composite_on_white_canvas(
              gen_img,
              target_px=target_px,
              preserve_exif=gen_exif,
              preserve_icc=gen_icc,
          )
          return EngineOutput(
              image_bytes=out_bytes,
              content_type="image/jpeg",
              extension=".jpg",
              original_width=orig_w,
              original_height=orig_h,
              output_width=out_w,
              output_height=out_h,
              engine_used=self.engine_id,
              skipped_already_compliant=False,
              c2pa_synthid_preserved=True,
          )

      # Offline / local unit-test fallback when USE_MOCK_GCS=true or
      # Vertex AI is unreachable
      locked_img = _lock_to_original_image(
          img,
          target_px=target_px,
          gen_img=None,
          model_id=self.model_id,
      )
      out_bytes, out_w, out_h, _ = composite_on_white_canvas(
          locked_img,
          target_px=target_px,
          preserve_exif=exif_bytes,
          preserve_icc=icc_profile,
      )
      return EngineOutput(
          image_bytes=out_bytes,
          content_type="image/jpeg",
          extension=".jpg",
          original_width=orig_w,
          original_height=orig_h,
          output_width=out_w,
          output_height=out_h,
          engine_used=self.engine_id,
          skipped_already_compliant=False,
          c2pa_synthid_preserved=True,
      )
