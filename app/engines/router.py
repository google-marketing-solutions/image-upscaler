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

"""Selectable Multi-Engine Router & SKU Engine Recommendation Logic."""

from __future__ import annotations

from typing import Dict

from app.config import ENGINE_CATALOG
from app.config import EngineSpec
from app.engines.base import BaseUpscaleEngine
from app.engines.base import EngineOutput
from app.engines.lanczos_canvas import LocalSmartLanczosEngine
from app.engines.realesrgan_gpu import RealEsrganGpuEngine
from app.engines.vertex_nano_banana import VertexNanoBananaEngine


class MultiEngineRouter:
  """Routes upscaling tasks to selected engines and recommends SKU engines."""

  def __init__(self) -> None:
    self._engines: Dict[str, BaseUpscaleEngine] = {
        "local-lanczos-canvas": LocalSmartLanczosEngine(),
        "realesrgan": RealEsrganGpuEngine(),
        "gemini-3.1-flash-lite-image": VertexNanoBananaEngine(
            "gemini-3.1-flash-lite-image"
        ),
        "gemini-3.1-flash-image": VertexNanoBananaEngine(
            "gemini-3.1-flash-image"
        ),
        "gemini-3-pro-image": VertexNanoBananaEngine("gemini-3-pro-image"),
    }

  def get_engine(self, engine_id: str) -> BaseUpscaleEngine:
    if engine_id not in self._engines:
      raise ValueError(
          f"Unknown engine_id '{engine_id}'. Supported engines: "
          f"{list(self._engines.keys())}"
      )
    return self._engines[engine_id]

  def get_spec(self, engine_id: str) -> EngineSpec:
    if engine_id not in ENGINE_CATALOG:
      raise ValueError(f"Unknown engine_id '{engine_id}'")
    return ENGINE_CATALOG[engine_id]

  def upscale(
      self,
      image_bytes: bytes,
      target_px: int,
      engine_id: str = "local-lanczos-canvas",
  ) -> EngineOutput:
    """Upscale image bytes to target_px using the specified engine."""
    if target_px not in (500, 1500):
      raise ValueError(
          "target_px must be either 500 (Compliance Mode) or 1500 "
          "(Best-Performance Mode)"
      )
    engine = self.get_engine(engine_id)
    return engine.upscale(image_bytes=image_bytes, target_px=target_px)

  @staticmethod
  def recommend_engine(
      width: int,
      height: int,
      has_text_or_logo: bool = True,
      is_hero_sku: bool = False,
      target_px: int = 500,
  ) -> str:
    """Recommend the optimal upscaling engine for a SKU image.

    Routing criteria:
    1. Any product containing Japanese packaging text, brand logos, or model
       numbers,
       OR any source image >=300x300 px -> `local-lanczos-canvas` (0%
       hallucination, $0.08-$0.16 / 10k).
    2. Flagship Hero SKUs (text-free) -> `gemini-3-pro-image` (Nano Banana
       Pro).
    3. Extreme low-resolution (<=250x250 px) text-free products:
       - If target_px == 1500 for high-margin visual SKUs ->
         `gemini-3.1-flash-image` (native 2K)
       - Otherwise -> `gemini-3.1-flash-lite-image` (Nano Banana 2 Lite,
         $0.0168/img Batch)
    4. Sub-300px SKUs needing non-generative sharpening -> `realesrgan` or
       `local-lanczos-canvas`.

    Args:
      width: Source image width in pixels.
      height: Source image height in pixels.
      has_text_or_logo: Whether the SKU image contains text or brand logos.
      is_hero_sku: Whether the SKU is a flagship hero product.
      target_px: Target square output resolution in pixels (500 or 1500).

    Returns:
      The recommended engine ID string.
    """
    if has_text_or_logo:
      return "local-lanczos-canvas"
    if is_hero_sku:
      return "gemini-3-pro-image"

    min_dim = min(width, height)
    if min_dim >= 300:
      return "local-lanczos-canvas"
    if min_dim <= 250:
      return (
          "gemini-3.1-flash-lite-image"
          if target_px == 500
          else "gemini-3.1-flash-image"
      )
    return "realesrgan"


engine_router = MultiEngineRouter()
