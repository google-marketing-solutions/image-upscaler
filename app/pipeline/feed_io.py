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
from app.models import MAX_ADDITIONAL_IMAGES
from app.models import SupplementalFeedRow

# Output CSV 3-column header specification: id,input_url,scaled_image_url
GMC_SUPPLEMENTAL_COLUMNS = ("id", "input_url", "scaled_image_url")
GMC_SUPPLEMENTAL_WITH_ADDITIONAL_COLUMNS = (
    "id",
    "input_url",
    "scaled_image_url",
    "additional_image_link",
    "additional_scaled_image_link",
)

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

_PRIMARY_IMAGE_HEADERS = {
    "image_link",
    "input_url",
    "image_url",
    "url",
    "image",
    "link",
    "image link",
    "src",
}

_ID_HEADERS = {
    "id",
    "sku",
    "offer_id",
    "item_id",
}


def _is_additional_image_header(header_lower: str) -> bool:
  """Return True if header matches GMC additional_image_link column variants."""
  if header_lower in (
      "additional_image_link",
      "additional_image_links",
      "additional image link",
      "additional image links",
  ):
    return True
  return bool(
      re.match(
          r"^additional[_\s]?image(?:[_\s]?links?)?[_\s]?\d+$", header_lower
      )
  )


def _clean_url_token(token: str) -> str:
  """Strip surrounding whitespace and ASCII/smart quotes from a URL token."""
  return (token or "").strip().strip("\"“”'").strip()


def _is_supported_url(token: str) -> bool:
  """Return True if token begins with http://, https://, or synthetic://."""
  cleaned = _clean_url_token(token)
  return cleaned.startswith(("http://", "https://", "synthetic://"))


def _split_additional_urls(raw_val: str) -> List[str]:
  """Split comma-, pipe-, or semicolon-delimited GMC image URLs.

  Supports standard comma-separated URLs, pipe-delimited URLs, `%2C`-encoded
  commas inside URLs, and GMC's quoted comma-separated URL syntax
  (`"https://example.com/img2,3.jpg","https://example.com/img2,4.jpg"`).

  Args:
    raw_val: Raw cell string containing one or more image URLs.

  Returns:
    List of cleaned individual URL strings.
  """
  if not raw_val or not raw_val.strip():
    return []
  normalized = raw_val.strip().replace("“", '"').replace("”", '"')
  parts = re.split(
      r'\s*[|,;]\s*(?=["\']?(?:https?://|synthetic://))',
      normalized,
  )
  urls: List[str] = []
  for part in parts:
    cleaned = _clean_url_token(part)
    if cleaned and _is_supported_url(cleaned):
      urls.append(cleaned)
  return urls


def parse_delimited_feed(
    content: str, delimiter: str = ","
) -> List[FeedInputRow]:
  """Parse CSV or TSV content containing primary and additional image URLs.

  Supports:
  1. Standard GMC feeds (`id`, `image_link`, `additional_image_link` /
     `additional_image_links`), including repeated `additional_image_link`
     columns, numbered columns (`additional_image_link_1`..`10`), comma- or
     pipe-separated URLs, and quoted URLs containing commas (up to 10
     additional images per SKU).
  2. Additional-only GMC supplemental feeds (`id,additional_image_link` where
     `image_link` is omitted or empty).
  3. Generic CSVs containing an `image_link`, `image_url`, `url`, or `image`
     column (auto-generates `id` if omitted).
  4. Headerless single- or multi-column CSVs of `http(s)://` or `synthetic://`
     URLs.

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

  # Case 4a: Headerless single-column list of URLs
  if _is_supported_url(first_line) and delimiter not in first_line:
    return [
        FeedInputRow(
            id=f"SKU-{idx + 1:04d}",
            image_link=_clean_url_token(line),
            additional_image_links=[],
        )
        for idx, line in enumerate(lines)
        if _is_supported_url(line)
    ]

  all_csv_rows = list(
      csv.reader(io.StringIO(cleaned_content), delimiter=delimiter)
  )
  if not all_csv_rows:
    raise ValueError("Feed content is empty")

  first_row_cells = all_csv_rows[0]
  # Case 4b: Headerless multi-column CSV where the first row already contains
  # a URL
  if any(_is_supported_url(c) for c in first_row_cells):
    headerless_rows: List[FeedInputRow] = []
    for idx, row_cells in enumerate(all_csv_rows):
      cleaned_cells = [c.strip() for c in row_cells if c and c.strip()]
      if not cleaned_cells:
        continue
      url_cells: List[str] = []
      non_url_cells: List[str] = []
      for cell in cleaned_cells:
        split_urls = _split_additional_urls(cell)
        if split_urls:
          url_cells.extend(split_urls)
        elif not _is_supported_url(cell):
          non_url_cells.append(cell)
      if not url_cells:
        continue
      sku_id = non_url_cells[0] if non_url_cells else f"SKU-{idx + 1:04d}"
      headerless_rows.append(
          FeedInputRow(
              id=sku_id,
              image_link=url_cells[0],
              additional_image_links=url_cells[1 : 1 + MAX_ADDITIONAL_IMAGES],
          )
      )
    if headerless_rows:
      return headerless_rows

  raw_headers = [(h or "").strip() for h in first_row_cells]
  if not any(raw_headers):
    raise ValueError("Feed is missing header columns")

  data_rows = all_csv_rows[1:]
  if not data_rows:
    raise ValueError("No valid product rows with an image URL found in CSV")

  low_headers = [h.lower() for h in raw_headers]
  id_indices = [i for i, h in enumerate(low_headers) if h in _ID_HEADERS]
  img_indices = [
      i for i, h in enumerate(low_headers) if h in _PRIMARY_IMAGE_HEADERS
  ]
  add_indices = [
      i for i, h in enumerate(low_headers) if _is_additional_image_header(h)
  ]

  if not img_indices and not add_indices:
    # Auto-detect the first column that contains an http(s):// or synthetic://
    # URL in the first non-empty data row
    for row_cells in data_rows:
      for col_idx, val in enumerate(row_cells):
        if _is_supported_url(val):
          img_indices = [col_idx]
          break
      if img_indices:
        break

  if not img_indices and not add_indices:
    raise ValueError(
        "CSV must include an image URL column ('image_link', "
        "'additional_image_link', 'image_url', 'url', or 'image')."
    )

  rows: List[FeedInputRow] = []
  num_headers = len(raw_headers)
  for idx, row_cells in enumerate(data_rows):
    if not row_cells or not any(c and c.strip() for c in row_cells):
      continue

    raw_id = ""
    for id_idx in id_indices:
      if id_idx < len(row_cells) and row_cells[id_idx].strip():
        raw_id = row_cells[id_idx].strip()
        break
    if not raw_id:
      raw_id = f"SKU-{idx + 1:04d}"

    primary_candidates: List[str] = []
    for img_idx in img_indices:
      if img_idx < len(row_cells):
        cell_val = row_cells[img_idx].strip()
        if cell_val:
          split_prim = _split_additional_urls(cell_val)
          if split_prim:
            primary_candidates.extend(split_prim)
          else:
            primary_candidates.append(_clean_url_token(cell_val))

    primary_url = primary_candidates[0] if primary_candidates else ""
    additional_urls: List[str] = list(primary_candidates[1:])

    for add_idx in add_indices:
      if add_idx < len(row_cells):
        cell_val = row_cells[add_idx].strip()
        if cell_val:
          additional_urls.extend(_split_additional_urls(cell_val))

    # Capture overflow cells beyond the header count when a user pastes
    # quoted comma-separated URLs like `"https://.../a,1.jpg","https://.../b"`
    if len(row_cells) > num_headers:
      for extra_cell in row_cells[num_headers:]:
        if extra_cell and extra_cell.strip():
          additional_urls.extend(_split_additional_urls(extra_cell))

    if not primary_url and not additional_urls:
      continue

    rows.append(
        FeedInputRow(
            id=raw_id,
            image_link=primary_url,
            additional_image_links=additional_urls[:MAX_ADDITIONAL_IMAGES],
        )
    )

  if not rows:
    raise ValueError("No valid product rows with an image URL found in CSV")
  return rows


def serialize_supplemental_feed(
    rows: Sequence[SupplementalFeedRow],
    delimiter: str = ",",
) -> str:
  """Serialize rows into GMC Supplemental Feed CSV format.

  Emits `id,input_url,scaled_image_url` when only primary images exist, or adds
  `additional_image_link,additional_scaled_image_link` when any row includes
  additional images.

  Args:
    rows: Sequence of SupplementalFeedRow records.
    delimiter: Output CSV delimiter character.

  Returns:
    Formatted CSV string.

  Raises:
    ValueError: If any non-empty scaled URL does not use the `https://` scheme.
  """
  buf = io.StringIO()
  writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
  has_additional = any(
      bool(
          (r.additional_image_link and r.additional_image_link.strip())
          or (
              r.additional_scaled_image_link
              and r.additional_scaled_image_link.strip()
          )
      )
      for r in rows
  )
  headers = (
      GMC_SUPPLEMENTAL_WITH_ADDITIONAL_COLUMNS
      if has_additional
      else GMC_SUPPLEMENTAL_COLUMNS
  )
  writer.writerow(headers)

  for row in rows:
    scaled_add = (
        row.additional_scaled_image_link or row.additional_image_link or ""
    )
    orig_add = row.additional_image_link or ""
    if not row.image_link and not scaled_add:
      raise ValueError(
          f"GMC Supplemental Feed row for SKU {row.id} has no image URLs"
      )
    if row.image_link and not row.image_link.startswith("https://"):
      raise ValueError(
          "GMC Supplemental Feed requires 'https://' URLs for "
          f"scaled_image_url (SKU {row.id}: {row.image_link})"
      )
    if scaled_add:
      for add_u in _split_additional_urls(scaled_add):
        if not add_u.startswith("https://"):
          raise ValueError(
              "GMC Supplemental Feed requires 'https://' URLs for "
              f"additional_scaled_image_link (SKU {row.id}: {add_u})"
          )
    if has_additional:
      writer.writerow([
          row.id,
          row.input_url,
          row.image_link,
          orig_add,
          scaled_add,
      ])
    else:
      writer.writerow([row.id, row.input_url, row.image_link])

  return buf.getvalue()


def ensure_output_csv_has_input_url(input_csv: str, output_csv: str) -> str:
  """Ensure output CSV has normalized GMC supplemental headers.

  Args:
    input_csv: Original input CSV text used for fallback URL lookup.
    output_csv: Existing output CSV text.

  Returns:
    Normalized 3- or 5-column CSV string.
  """
  if not output_csv or not output_csv.strip():
    return output_csv
  first_line = output_csv.strip().splitlines()[0].strip()
  if first_line in (
      ",".join(GMC_SUPPLEMENTAL_COLUMNS),
      ",".join(GMC_SUPPLEMENTAL_WITH_ADDITIONAL_COLUMNS),
  ):
    return output_csv
  try:
    in_map: Dict[str, str] = {}
    in_add_map: Dict[str, str] = {}
    if input_csv and input_csv.strip():
      in_rows = parse_delimited_feed(input_csv)
      in_map = {r.id: r.image_link for r in in_rows}
      in_add_map = {
          r.id: ",".join(r.additional_image_links)
          for r in in_rows
          if r.additional_image_links
      }
    reader = list(csv.DictReader(io.StringIO(output_csv.strip())))
    has_additional = any(
        bool(
            (r.get("additional_image_link") or "").strip()
            or (r.get("additional_scaled_image_link") or "").strip()
        )
        for r in reader
    )
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", lineterminator="\n")
    writer.writerow(
        GMC_SUPPLEMENTAL_WITH_ADDITIONAL_COLUMNS
        if has_additional
        else GMC_SUPPLEMENTAL_COLUMNS
    )
    for out_row in reader:
      sku_id = (out_row.get("id") or "").strip()
      orig_url = (out_row.get("input_url") or "").strip() or in_map.get(
          sku_id, ""
      )
      scaled_url = (out_row.get("scaled_image_url") or "").strip() or (
          out_row.get("image_link") or ""
      ).strip()
      raw_add = (out_row.get("additional_image_link") or "").strip()
      raw_scaled_add = (
          out_row.get("additional_scaled_image_link") or ""
      ).strip()
      if raw_scaled_add:
        orig_add = raw_add or in_add_map.get(sku_id, "")
        scaled_add = raw_scaled_add
      else:
        scaled_add = raw_add
        orig_add = in_add_map.get(sku_id, "") or raw_add
      if has_additional:
        writer.writerow([sku_id, orig_url, scaled_url, orig_add, scaled_add])
      else:
        writer.writerow([sku_id, orig_url, scaled_url])
    return buf.getvalue()
  except Exception:  # pylint: disable=broad-exception-caught
    return output_csv


def serialize_gmc_supplemental_only_csv(input_csv: str, output_csv: str) -> str:
  """Convert output CSV into strict GMC Supplemental Feed format.

  Emits `id,image_link,additional_image_link` where `image_link` and
  `additional_image_link` contain only the updated/upscaled URLs.

  Args:
    input_csv: Original input CSV text.
    output_csv: Existing output CSV text.

  Returns:
    Formatted 3-column GMC Supplemental Feed CSV string.
  """
  normalized = ensure_output_csv_has_input_url(input_csv, output_csv)
  if not normalized or not normalized.strip():
    return "id,image_link,additional_image_link\n"
  reader = list(csv.DictReader(io.StringIO(normalized.strip())))
  buf = io.StringIO()
  writer = csv.writer(buf, delimiter=",", lineterminator="\n")
  writer.writerow(["id", "image_link", "additional_image_link"])
  for out_row in reader:
    sku_id = (out_row.get("id") or "").strip()
    scaled_url = (out_row.get("scaled_image_url") or "").strip() or (
        out_row.get("image_link") or ""
    ).strip()
    scaled_add = (
        out_row.get("additional_scaled_image_link")
        or out_row.get("additional_image_link")
        or ""
    ).strip()
    writer.writerow([sku_id, scaled_url, scaled_add])
  return buf.getvalue()


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
