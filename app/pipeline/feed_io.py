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

"""GMC Feed Ingestion and Strict 3-Column Supplemental Feed Exporter."""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, Iterable, List, Sequence

from app.models import ConversionLogEntry
from app.models import FeedInputRow
from app.models import SupplementalFeedRow

# Output CSV 3-column header specification: id,input_url,scaled_image_url
GMC_SUPPLEMENTAL_COLUMNS = ("id", "input_url", "scaled_image_url")

# Daily conversion log CSV header specification for
# `gs://<project_dir>/status/{YYYYMMDD}/log.csv`
DAILY_STATUS_LOG_COLUMNS = (
    "timestamp_utc",
    "date_yyyymmdd",
    "request_id",
    "sku_id",
    "source_image_url",
    "converted_gcs_url",
    "engine_id",
    "target_resolution",
    "original_dimensions",
    "output_dimensions",
    "status",
    "processing_ms",
)


def _split_additional_urls(raw_val: str) -> List[str]:
  """Split comma- or pipe-delimited additional_image_link values into URLs."""
  if not raw_val or not raw_val.strip():
    return []
  parts = re.split(r"\s*[|,]\s*(?=https?://|synthetic://)", raw_val.strip())
  return [p.strip() for p in parts if p and p.strip()]


def parse_delimited_feed(
    content: str, delimiter: str = ","
) -> List[FeedInputRow]:
  """Parse CSV or TSV content containing image URLs.

  Supports:
  1. Standard GMC feeds (`id`, `image_link`, `additional_image_link` /
     `additional_image_links`)
  2. Generic CSVs containing an `image_link`, `image_url`, `url`, or `image`
     column (auto-generates `id` if omitted)
  3. Headerless single-column CSVs of `http(s)://` or `synthetic://` URLs

  Args:
    content: Raw CSV or TSV text content.
    delimiter: Field delimiter character (default `,`).

  Returns:
    List of parsed FeedInputRow objects.

  Raises:
    ValueError: If feed content is empty or lacks valid image URL columns/rows.
  """
  if not content or not content.strip():
    raise ValueError("Feed content is empty")

  cleaned_content = content.strip()
  lines = [ln.strip() for ln in cleaned_content.splitlines() if ln.strip()]
  if not lines:
    raise ValueError("Feed content is empty")

  first_line = lines[0]
  if "\t" in first_line and "," not in first_line:
    delimiter = "\t"

  # Case 3a: Headerless single-column list of URLs
  if (
      first_line.startswith(("http://", "https://", "synthetic://"))
      and delimiter not in first_line
  ):
    return [
        FeedInputRow(
            id=f"SKU-{idx + 1:04d}",
            image_link=line,
            additional_image_links=[],
        )
        for idx, line in enumerate(lines)
        if line.startswith(("http://", "https://", "synthetic://"))
    ]

  # Case 3b: Headerless multi-column CSV where the first row already contains
  # a URL
  first_row_cells = next(
      csv.reader(io.StringIO(first_line), delimiter=delimiter), []
  )
  if any(
      c.strip().startswith(("http://", "https://", "synthetic://"))
      for c in first_row_cells
  ):
    headerless_rows: List[FeedInputRow] = []
    for idx, row_cells in enumerate(
        csv.reader(io.StringIO(cleaned_content), delimiter=delimiter)
    ):
      cleaned_cells = [c.strip() for c in row_cells if c and c.strip()]
      if not cleaned_cells:
        continue
      url_cells = [
          c
          for c in cleaned_cells
          if c.startswith(("http://", "https://", "synthetic://"))
      ]
      if not url_cells:
        continue
      non_url_cells = [
          c
          for c in cleaned_cells
          if not c.startswith(("http://", "https://", "synthetic://"))
      ]
      sku_id = non_url_cells[0] if non_url_cells else f"SKU-{idx + 1:04d}"
      headerless_rows.append(
          FeedInputRow(
              id=sku_id,
              image_link=url_cells[0],
              additional_image_links=url_cells[1:],
          )
      )
    if headerless_rows:
      return headerless_rows

  reader = csv.DictReader(io.StringIO(cleaned_content), delimiter=delimiter)
  if not reader.fieldnames:
    raise ValueError("Feed is missing header columns")

  raw_dict_rows = list(reader)
  if not raw_dict_rows:
    raise ValueError("No valid product rows with an image URL found in CSV")

  norm_headers = {h.strip().lower(): h for h in reader.fieldnames if h}
  img_col = (
      norm_headers.get("image_link")
      or norm_headers.get("input_url")
      or norm_headers.get("image_url")
      or norm_headers.get("url")
      or norm_headers.get("image")
      or norm_headers.get("link")
      or norm_headers.get("image link")
      or norm_headers.get("src")
  )
  if not img_col:
    # Auto-detect the first column that contains an http(s):// URL
    for h in reader.fieldnames:
      if not h:
        continue
      val = (raw_dict_rows[0].get(h) or "").strip()
      if val.startswith(("http://", "https://", "synthetic://")):
        img_col = h
        break

  if not img_col:
    raise ValueError(
        "CSV must include an image URL column ('image_link', 'image_url',"
        " 'url', or 'image')."
    )

  id_col = (
      norm_headers.get("id")
      or norm_headers.get("sku")
      or norm_headers.get("offer_id")
      or norm_headers.get("item_id")
  )
  add_cols = [
      orig_name
      for low_name, orig_name in norm_headers.items()
      if low_name in ("additional_image_link", "additional_image_links")
  ]

  rows: List[FeedInputRow] = []
  for idx, raw_row in enumerate(raw_dict_rows):
    raw_img = (raw_row.get(img_col) or "").strip()
    if not raw_img:
      continue
    raw_id = (
        (raw_row.get(id_col) or "").strip() if id_col else f"SKU-{idx + 1:04d}"
    )
    if not raw_id:
      raw_id = f"SKU-{idx + 1:04d}"

    additional_urls: List[str] = []
    for col in add_cols:
      cell = raw_row.get(col) or ""
      additional_urls.extend(_split_additional_urls(cell))

    rows.append(
        FeedInputRow(
            id=raw_id,
            image_link=raw_img,
            additional_image_links=additional_urls,
        )
    )

  if not rows:
    raise ValueError("No valid product rows with an image URL found in CSV")
  return rows


def serialize_supplemental_feed(
    rows: Sequence[SupplementalFeedRow],
    delimiter: str = ",",
) -> str:
  """Serialize rows into Output CSV with 3-column GMC supplemental headers.

  Args:
    rows: Sequence of SupplementalFeedRow records.
    delimiter: Output CSV delimiter character.

  Returns:
    Formatted CSV string with `id,input_url,scaled_image_url` header.

  Raises:
    ValueError: If any scaled_image_url does not use the `https://` scheme.
  """
  buf = io.StringIO()
  writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
  writer.writerow(GMC_SUPPLEMENTAL_COLUMNS)

  for row in rows:
    if not row.image_link.startswith("https://"):
      raise ValueError(
          "GMC Supplemental Feed requires 'https://' URLs for "
          f"scaled_image_url (SKU {row.id}: {row.image_link})"
      )
    writer.writerow([row.id, row.input_url, row.image_link])

  return buf.getvalue()


def ensure_output_csv_has_input_url(input_csv: str, output_csv: str) -> str:
  """Ensure output CSV has exact `id,input_url,scaled_image_url` headers.

  Args:
    input_csv: Original input CSV text used for fallback URL lookup.
    output_csv: Existing output CSV text.

  Returns:
    Normalized 3-column CSV string.
  """
  if not output_csv or not output_csv.strip():
    return output_csv
  first_line = output_csv.strip().splitlines()[0].strip()
  if first_line == ",".join(GMC_SUPPLEMENTAL_COLUMNS):
    return output_csv
  try:
    in_map: Dict[str, str] = {}
    if input_csv and input_csv.strip():
      in_rows = parse_delimited_feed(input_csv)
      in_map = {r.id: r.image_link for r in in_rows}
    reader = csv.DictReader(io.StringIO(output_csv.strip()))
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", lineterminator="\n")
    writer.writerow(GMC_SUPPLEMENTAL_COLUMNS)
    for out_row in reader:
      sku_id = (out_row.get("id") or "").strip()
      orig_url = (out_row.get("input_url") or "").strip() or in_map.get(
          sku_id, ""
      )
      scaled_url = (out_row.get("scaled_image_url") or "").strip() or (
          out_row.get("image_link") or ""
      ).strip()
      writer.writerow([sku_id, orig_url, scaled_url])
    return buf.getvalue()
  except Exception:  # pylint: disable=broad-exception-caught
    return output_csv


def serialize_daily_status_log(
    entries: Sequence[ConversionLogEntry],
    include_header: bool = True,
) -> str:
  """Serialize ConversionLogEntry rows into CSV format for daily status logs.

  Args:
    entries: Sequence of ConversionLogEntry records.
    include_header: Whether to include the CSV header row.

  Returns:
    Serialized CSV string.
  """
  buf = io.StringIO()
  writer = csv.writer(buf, delimiter=",", lineterminator="\n")
  if include_header:
    writer.writerow(DAILY_STATUS_LOG_COLUMNS)
  for e in entries:
    writer.writerow([
        e.timestamp_utc,
        e.date_yyyymmdd,
        e.request_id,
        e.sku_id,
        e.source_image_url,
        e.converted_gcs_url,
        e.engine_id,
        e.target_resolution,
        e.original_dimensions,
        e.output_dimensions,
        e.status,
        e.processing_ms,
    ])
  return buf.getvalue()


def load_google_sheet_feed(
    spreadsheet_id_or_url: str, worksheet_name: str = ""
) -> List[FeedInputRow]:
  """Load product feed rows from a Google Sheet via ADC (`gspread`).

  Args:
    spreadsheet_id_or_url: Google Sheet ID or full HTTPS URL.
    worksheet_name: Optional worksheet tab name.

  Returns:
    List of parsed FeedInputRow objects.
  """
  import gspread  # pylint: disable=g-import-not-at-top

  gc = gspread.service_account()
  sh = (
      gc.open_by_url(spreadsheet_id_or_url)
      if spreadsheet_id_or_url.startswith("https://")
      else gc.open_by_key(spreadsheet_id_or_url)
  )
  ws = sh.worksheet(worksheet_name) if worksheet_name else sh.sheet1
  records: List[Dict[str, str]] = ws.get_all_records()

  buf = io.StringIO()
  writer = csv.DictWriter(
      buf, fieldnames=["id", "image_link", "additional_image_link"]
  )
  writer.writeheader()
  for rec in records:
    norm = {str(k).strip().lower(): str(v).strip() for k, v in rec.items()}
    writer.writerow({
        "id": norm.get("id", ""),
        "image_link": norm.get("image_link", ""),
        "additional_image_link": (
            norm.get("additional_image_link")
            or norm.get("additional_image_links", "")
        ),
    })
  return parse_delimited_feed(buf.getvalue())


def export_supplemental_feed_to_google_sheet(
    rows: Iterable[SupplementalFeedRow],
    spreadsheet_id_or_url: str,
    worksheet_name: str = "GMC_Supplemental_Feed",
) -> str:
  """Write the GMC Supplemental Feed to a Google Sheet tab.

  Args:
    rows: Iterable of SupplementalFeedRow records.
    spreadsheet_id_or_url: Google Sheet ID or full HTTPS URL.
    worksheet_name: Target worksheet tab name.

  Returns:
    URL of the updated Google Sheet.
  """
  import gspread  # pylint: disable=g-import-not-at-top

  gc = gspread.service_account()
  sh = (
      gc.open_by_url(spreadsheet_id_or_url)
      if spreadsheet_id_or_url.startswith("https://")
      else gc.open_by_key(spreadsheet_id_or_url)
  )
  try:
    ws = sh.worksheet(worksheet_name)
    ws.clear()
  except Exception:  # pylint: disable=broad-exception-caught
    ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=3)

  matrix = [list(GMC_SUPPLEMENTAL_COLUMNS)]
  for row in rows:
    matrix.append([row.id, row.input_url, row.image_link])
  ws.update("A1", matrix)
  return sh.url
