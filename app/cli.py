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

"""CLI & Cloud Run Job Entrypoint for ImageUpscaler.

Supports:
1. `python -m app.cli audit --input feed.csv`
   Runs the Stage 1 Dimension Auditor and prints the sub-500x500 exposure &
   5-engine Cost Matrix.
2. `python -m app.cli upscale --input feed.csv --output supplemental_feed.csv
   --resolution 500 --engine local-lanczos-canvas`
   Executes the 3-Tier Parallel Upscaling Pipeline (respecting
   `CLOUD_RUN_TASK_INDEX` and `CLOUD_RUN_TASK_COUNT`
   for sharded Cloud Run Jobs) and writes the strict 3-column GMC Supplemental
   Feed.
3. `python -m app.cli serve --host 127.0.0.1 --port 8080`
   Starts the Web UI & API server (defaults to 127.0.0.1 for secure local
   testing).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
from typing import Sequence

from app.config import ENGINE_CATALOG
from app.config import settings
from app.pipeline.auditor import audit_catalog_feed
from app.pipeline.feed_io import export_supplemental_feed_to_google_sheet
from app.pipeline.feed_io import load_google_sheet_feed
from app.pipeline.feed_io import parse_delimited_feed
from app.pipeline.feed_io import serialize_supplemental_feed
from app.pipeline.gcs_uploader import PublicGcsUploader
from app.pipeline.orchestrator import run_upscale_pipeline
from app.pipeline.synthetic_catalog import SAMPLE_ANCHOR_FEED_CSV
import uvicorn


def _load_input_rows(input_source: str, sheet_tab: str = ""):
  """Load feed input rows from sample, Google Sheet, or local file."""
  if input_source == "sample":
    return parse_delimited_feed(SAMPLE_ANCHOR_FEED_CSV)
  if input_source.startswith(
      "https://docs.google.com/spreadsheets/"
  ) or input_source.startswith("gsheet:"):
    sheet_key = input_source.removeprefix("gsheet:")
    return load_google_sheet_feed(sheet_key, worksheet_name=sheet_tab)
  path = pathlib.Path(input_source).resolve()
  content = path.read_text(encoding="utf-8-sig")
  return parse_delimited_feed(content)


async def _cmd_audit(args: argparse.Namespace) -> int:
  """Execute the Stage 1 catalog dimension audit CLI command."""
  rows = _load_input_rows(args.input, args.sheet_tab)
  report = await audit_catalog_feed(rows)
  if args.json:
    print(json.dumps(report.model_dump(), indent=2))
    return 0

  print("=" * 88)
  print("GMC ImageUpscaler — Stage 1 Catalog Dimension Audit & Cost Matrix")
  print("=" * 88)
  print(f"Total SKUs Audited         : {report.total_skus}")
  print(f"Total Image References     : {report.total_image_references}")
  print(
      f"Unique Deduplicated URLs   : {report.unique_image_urls} "
      f"({report.deduplication_savings_pct}% dedup savings)"
  )
  print(
      f"Sub-500x500 Disapproval Risk: {report.sub_500x500_urls} URLs "
      f"({report.sub_500x500_exposure_pct}% exposure)"
  )
  print(f"500x500 - 1499x1499 URLs   : {report.between_500_and_1499_urls} URLs")
  print(f">=1500x1500 Compliant URLs : {report.compliant_1500x1500_urls} URLs")
  print("-" * 88)
  print(
      f"{'Engine ID':<30} | {'Role':<26} | {'500px Batch ($)':<15} | "
      f"{'1500px Batch ($)':<15}"
  )
  print("-" * 88)
  for est in report.cost_matrix:
    print(
        f"{est.engine_id:<30} | {est.role:<26} | "
        f"${est.cost_500_batch_usd:<14.6f} | "
        f"${est.cost_1500_batch_usd:<14.6f}"
    )
  print("=" * 88)
  return 0


async def _cmd_upscale(args: argparse.Namespace) -> int:
  """Execute the parallel catalog upscaling CLI command."""
  rows = _load_input_rows(args.input, args.sheet_tab)
  uploader = PublicGcsUploader(
      bucket_name=args.bucket or settings.public_gcs_bucket,
      use_mock_gcs=args.mock_gcs,
  )
  report = await run_upscale_pipeline(
      rows=rows,
      target_px=args.resolution,
      engine_id=args.engine,
      uploader=uploader,
      shard_index=args.shard_index,
      shard_count=args.shard_count,
  )

  delimiter = "\t" if args.output.endswith(".tsv") else ","
  serialized = serialize_supplemental_feed(
      report.supplemental_feed_rows, delimiter=delimiter
  )

  if args.output.startswith("gsheet:"):
    sheet_key = args.output.removeprefix("gsheet:")
    sheet_url = export_supplemental_feed_to_google_sheet(
        report.supplemental_feed_rows,
        spreadsheet_id_or_url=sheet_key,
        worksheet_name=args.output_sheet_tab,
    )
    print(f"Exported GMC Supplemental Feed to Google Sheet: {sheet_url}")
  else:
    out_path = pathlib.Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialized, encoding="utf-8")
    print(
        f"[{report.job_id}] Wrote {report.total_skus_processed} SKUs "
        f"({report.urls_upscaled} upscaled, "
        f"{report.urls_skipped_already_compliant} skipped) "
        f"to {out_path} in {report.total_duration_ms} ms (Est. cost: "
        f"${report.estimated_cost_usd:.6f})"
    )
  return 0


def build_parser() -> argparse.ArgumentParser:
  """Construct and return the CLI argument parser."""
  parser = argparse.ArgumentParser(
      prog="image-upscaler",
      description=(
          "Automated GMC Product Image Super-Resolution & Compliance Pipeline"
      ),
  )
  sub = parser.add_subparsers(dest="command", required=True)

  audit_p = sub.add_parser(
      "audit", help="Run Stage 1 Dimension Auditor & Cost Matrix Calculator"
  )
  audit_p.add_argument(
      "--input",
      default="sample",
      help="Path to CSV/TSV file, Google Sheet URL, or 'sample'",
  )
  audit_p.add_argument(
      "--sheet-tab",
      default="",
      help="Optional Google Sheet worksheet tab name",
  )
  audit_p.add_argument(
      "--json", action="store_true", help="Output audit report as JSON"
  )

  up_p = sub.add_parser(
      "upscale",
      help=(
          "Run 3-Tier Parallel Upscaling & export 3-column GMC "
          "Supplemental Feed"
      ),
  )
  up_p.add_argument(
      "--input",
      default="sample",
      help="Path to CSV/TSV file, Google Sheet URL, or 'sample'",
  )
  up_p.add_argument(
      "--sheet-tab", default="", help="Optional input Google Sheet tab name"
  )
  up_p.add_argument(
      "--output",
      default="./output/gmc_supplemental_feed.csv",
      help="Output CSV/TSV path or gsheet:<id>",
  )
  up_p.add_argument(
      "--output-sheet-tab",
      default="GMC_Supplemental_Feed",
      help="Output Google Sheet tab name",
  )
  up_p.add_argument(
      "--resolution",
      type=int,
      choices=[500, 1500],
      default=500,
      help="Target resolution (500 or 1500)",
  )
  up_p.add_argument(
      "--engine",
      choices=list(ENGINE_CATALOG.keys()),
      default="local-lanczos-canvas",
      help="Upscaling engine ID",
  )
  up_p.add_argument("--bucket", default="", help="Public GCS Bucket name")
  up_p.add_argument(
      "--mock-gcs",
      action="store_true",
      default=settings.use_mock_gcs,
      help="Use local sandbox preview storage instead of live GCS upload",
  )
  up_p.add_argument(
      "--shard-index",
      type=int,
      default=None,
      help="Override CLOUD_RUN_TASK_INDEX",
  )
  up_p.add_argument(
      "--shard-count",
      type=int,
      default=None,
      help="Override CLOUD_RUN_TASK_COUNT",
  )

  serve_p = sub.add_parser(
      "serve", help="Start the FastAPI Web UI & API server"
  )
  serve_p.add_argument(
      "--host",
      default="127.0.0.1",
      help="Bind host (defaults to 127.0.0.1 for local security)",
  )
  serve_p.add_argument("--port", type=int, default=8080, help="Bind port")

  return parser


def main(argv: Sequence[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  if args.command == "audit":
    return asyncio.run(_cmd_audit(args))
  if args.command == "upscale":
    return asyncio.run(_cmd_upscale(args))
  if args.command == "serve":
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
    return 0
  return 1


if __name__ == "__main__":
  raise SystemExit(main())
