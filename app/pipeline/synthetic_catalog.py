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

"""Synthetic product image & feed generator for testing and demos."""

from __future__ import annotations

import io
from typing import Tuple
import urllib.parse

from PIL import Image
from PIL import ImageDraw

SAMPLE_ANCHOR_FEED_CSV = (
    "id,image_link,additional_image_link\n"
    "RET-COSM-001,synthetic://retail-jp/RET-COSM-001/320x240/"
    "botanical_serum_jp_label.jpg,"
    "synthetic://retail-jp/RET-COSM-001/280x280/"
    "botanical_back_ingredients.jpg\n"
    "RET-COSM-001-VAR2,synthetic://retail-jp/RET-COSM-001/320x240/"
    "botanical_serum_jp_label.jpg,"
    "synthetic://retail-jp/RET-COSM-001/300x300/botanical_box.jpg\n"
    "IND-PART-1042,synthetic://industrial-supply/IND-PART-1042/360x260/"
    "steel_bearing_6204zz.jpg,"
    "synthetic://industrial-supply/IND-PART-1042/340x240/"
    "bearing_spec_chart.jpg\n"
    "REG-GIFT-8801,synthetic://regional-gifts/REG-GIFT-8801/220x220/"
    "premium_wagyu_gift.jpg,"
    "synthetic://regional-gifts/REG-GIFT-8801/240x180/wagyu_marbling.jpg\n"
    "MKT-APP-5510,synthetic://marketplace-apparel/MKT-APP-5510/200x240/"
    "vintage_leather_bag.jpg,\n"
    "RET-ELEC-9002,synthetic://retail-jp/RET-ELEC-9002/640x640/"
    "ionic_hair_dryer_eh.jpg,"
    "synthetic://retail-jp/RET-ELEC-9002/420x420/dryer_nozzle_attachment.jpg\n"
    "IND-PRO-7777,synthetic://industrial-supply/IND-PRO-7777/1600x1600/"
    "cordless_impact_driver_18v.jpg,\n"
)


def parse_synthetic_url_dimensions(url: str) -> Tuple[int, int, str, str]:
  """Extract (width, height, merchant, label) from a `synthetic://` URL."""
  parsed = urllib.parse.urlparse(url)
  merchant = parsed.netloc or "merchant"
  parts = [p for p in parsed.path.strip("/").split("/") if p]
  sku = parts[0] if len(parts) > 0 else "SKU-001"
  dim_part = parts[1] if len(parts) > 1 else "320x240"
  label = parts[2] if len(parts) > 2 else "product.jpg"

  try:
    w_str, h_str = dim_part.lower().split("x", 1)
    width = max(32, min(4096, int(w_str)))
    height = max(32, min(4096, int(h_str)))
  except ValueError:
    width, height = 320, 240

  return width, height, f"{merchant.upper()}:{sku}", label


def generate_synthetic_product_image(url: str) -> bytes:
  """Render a deterministic product thumbnail with packaging text & shapes."""
  width, height, title, subtitle = parse_synthetic_url_dimensions(url)

  # Pick deterministic palette from URL hash
  palette_seed = sum(ord(c) for c in url)
  palettes = [
      ((244, 246, 250), (30, 64, 175), (219, 234, 254)),
      ((250, 245, 240), (154, 52, 18), (254, 215, 170)),
      ((242, 249, 245), (22, 101, 52), (187, 247, 208)),
      ((248, 245, 252), (107, 33, 168), (233, 213, 255)),
  ]
  bg_color, primary_color, accent_color = palettes[palette_seed % len(palettes)]

  img = Image.new("RGB", (width, height), bg_color)
  draw = ImageDraw.Draw(img)

  # Draw outer product card border and inner product silhouette
  pad_x = max(8, width // 10)
  pad_y = max(8, height // 10)
  draw.rectangle(
      [pad_x, pad_y, width - pad_x - 1, height - pad_y - 1],
      fill=accent_color,
      outline=primary_color,
      width=max(2, min(width, height) // 80),
  )

  # Draw simulated product package header bar
  bar_h = max(14, (height - 2 * pad_y) // 4)
  draw.rectangle(
      [pad_x, pad_y, width - pad_x - 1, pad_y + bar_h],
      fill=primary_color,
  )

  # Render crisp packaging text & resolution badge
  draw.text((pad_x + 6, pad_y + 3), title[:22], fill=(255, 255, 255))
  draw.text(
      (pad_x + 6, pad_y + bar_h + 8),
      f"ORIG: {width}x{height}px",
      fill=primary_color,
  )
  draw.text((pad_x + 6, pad_y + bar_h + 24), subtitle[:24], fill=(40, 40, 40))

  out = io.BytesIO()
  img.save(out, format="JPEG", quality=92, subsampling=0)
  return out.getvalue()
