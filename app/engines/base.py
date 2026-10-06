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

"""Base upscaling engine abstraction and shared canvas/metadata utilities."""

from __future__ import annotations

import abc
import dataclasses
import io
from typing import Optional, Tuple

from PIL import Image
from PIL import ImageOps


@dataclasses.dataclass
class EngineOutput:
  """Output produced by an upscaling engine."""

  image_bytes: bytes
  content_type: str
  extension: str
  original_width: int
  original_height: int
  output_width: int
  output_height: int
  engine_used: str
  skipped_already_compliant: bool
  c2pa_synthid_preserved: bool = False


def get_image_dimensions_and_format(image_bytes: bytes) -> Tuple[int, int, str]:
  """Read image width, height, and format using Pillow.

  Args:
    image_bytes: Raw source image bytes.

  Returns:
    A tuple of (width, height, format_uppercase).
  """
  with Image.open(io.BytesIO(image_bytes)) as img:
    img_oriented = ImageOps.exif_transpose(img)
    width, height = img_oriented.size
    fmt = (img.format or "JPEG").upper()
    return width, height, fmt


def meets_target_resolution(width: int, height: int, target_px: int) -> bool:
  """Return True if both width and height already meet or exceed target_px."""
  return width >= target_px and height >= target_px


def composite_on_white_canvas(
    img: Image.Image,
    target_px: int,
    preserve_exif: Optional[bytes] = None,
    preserve_icc: Optional[bytes] = None,
) -> Tuple[bytes, int, int, bool]:
  """Place an image onto a pure white RGB canvas of target_px x target_px.

  Preserves aspect ratio by scaling the longest side to target_px using
  LANCZOS resampling, handles RGBA/P transparency by alpha-compositing over
  white, and preserves EXIF/ICC/C2PA metadata.

  Args:
    img: Source Pillow Image instance.
    target_px: Target square canvas edge length in pixels.
    preserve_exif: Optional raw EXIF metadata bytes to embed.
    preserve_icc: Optional raw ICC color profile bytes to embed.

  Returns:
    A tuple of (jpeg_bytes, output_width, output_height, metadata_preserved).
  """
  src_w, src_h = img.size
  scale = target_px / float(max(src_w, src_h))
  new_w = max(1, min(target_px, int(round(src_w * scale))))
  new_h = max(1, min(target_px, int(round(src_h * scale))))

  # Flatten transparency onto pure white before resizing for clean
  # anti-aliased edges
  if img.mode in ("RGBA", "LA") or (
      img.mode == "P" and "transparency" in img.info
  ):
    rgba = img.convert("RGBA")
    white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    composited = Image.alpha_composite(white_bg, rgba).convert("RGB")
  else:
    composited = img.convert("RGB")

  resized = composited.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
  canvas = Image.new("RGB", (target_px, target_px), (255, 255, 255))
  offset_x = (target_px - new_w) // 2
  offset_y = (target_px - new_h) // 2
  canvas.paste(resized, (offset_x, offset_y))

  out_buf = io.BytesIO()
  save_kwargs = {
      "format": "JPEG",
      "quality": 95,
      "subsampling": 0,
      "optimize": True,
  }
  c2pa_preserved = False
  if preserve_exif:
    save_kwargs["exif"] = preserve_exif
    c2pa_preserved = True
  if preserve_icc:
    save_kwargs["icc_profile"] = preserve_icc
    c2pa_preserved = True

  canvas.save(out_buf, **save_kwargs)
  return out_buf.getvalue(), target_px, target_px, c2pa_preserved


class BaseUpscaleEngine(abc.ABC):
  """Abstract base class for all ImageUpscaler engines."""

  engine_id: str

  @abc.abstractmethod
  def upscale(self, image_bytes: bytes, target_px: int) -> EngineOutput:
    """Upscale raw image bytes to target_px x target_px."""
    raise NotImplementedError
