/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * ImageUpscaler Google-Style SPA Client (`app.js`)
 *
 * Features:
 * - Default language: Japanese (`ja`), with 1-click APAC language pills (`日本語`,
 * `EN`, `中文`, `한국어`)
 *   and full APAC dropdown (`ja`, `en`, `zh-CN`, `zh-TW`, `ko`, `vi`, `th`,
 *   `id`, `hi`)
 * - Instant non-blocking background job submission (`async_job: true`) with
 * live auto-polling
 * - Automatic clearing of previous CSV state whenever opening "+ Raise New
 * Request"
 * - Dedicated `/request/<request-id>` and `/billing` views with cost & time
 * estimation per engine
 * - Strict XSS & session security:
 *   - Zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, or `document.write`
 *   - Zero `localStorage` or `sessionStorage` token storage
 *   - Zero native `alert()`, `confirm()`, or `prompt()` calls
 *   - All DOM nodes built with `document.createElement`, `textContent`, and
 *   `replaceChildren`
 */

(function () {
  "use strict";

  let csrfToken = "";
  let activeAccessCode = "";
  let todayYyyymmdd = "";
  let currentLang = "ja"; // Japanese as default
  let cachedRequests = [];
  let hasLoadedRequestsOnce = false;
  let pollTimerId = null;

  // Wizard state (cleared every time Wizard opens)
  let selectedFilename = "";
  let selectedCsvText = "";
  let detectedRowCount = 0;
  let selectedEngine = "local-lanczos-canvas";
  let selectedResolution = 1500;

  // Currently viewed request ID on `/request/<request-id>`
  let currentDetailRequestId = "";
  let detailCurrentRequest = null;
  let detailItemsCache = [];
  let detailStatusFilter = "ALL";
  let detailSearchQuery = "";
  let detailCurrentPage = 1;
  let detailPageSize = 25;

  // Dashboard Requests table pagination state
  let requestsCurrentPage = 1;
  let requestsPageSize = 20;

  // Currently inspected SKU inside #sku-detail-modal
  let activeModalSkuId = "";

  // Unit costs & throughput specs per image (dynamically synced with
  // /api/pricing)
  const ENGINE_SPECS = [
    {
      id: "local-lanczos-canvas",
      name: "Local Smart Lanczos (CPU)",
      cost500: 0.000018,
      cost1500: 0.000025,
      imgsPerSec: 140,
      bestForKey: "eng_lc_desc",
    },
    {
      id: "realesrgan",
      name: "Real-ESRGAN (GPU)",
      cost500: 0.00034,
      cost1500: 0.00049,
      imgsPerSec: 90,
      bestForKey: "eng_esr_desc",
    },
    {
      id: "gemini-3.1-flash-lite-image",
      name: "Vertex AI · Gemini 3.1 Flash Lite",
      cost500: 0.011,
      cost1500: 0.017,
      imgsPerSec: 18,
      bestForKey: "eng_gfl_desc",
    },
    {
      id: "gemini-3.1-flash-image",
      name: "Vertex AI · Gemini 3.1 Flash",
      cost500: 0.022,
      cost1500: 0.034,
      imgsPerSec: 15,
      bestForKey: "eng_gf_desc",
    },
    {
      id: "gemini-3-pro-image",
      name: "Vertex AI · Gemini 3 Pro Image",
      cost500: 0.067,
      cost1500: 0.067,
      imgsPerSec: 10,
      bestForKey: "eng_gp_desc",
    },
  ];

  const ENGINE_LABELS = {};
  const ENGINE_COST_MAP = {};
  ENGINE_SPECS.forEach(function (s) {
    ENGINE_LABELS[s.id] = s.name;
    ENGINE_COST_MAP[s.id] = s;
  });

  // Comprehensive APAC Multi-Language Dictionary (Default: Japanese `ja`)
  const I18N = {
    ja: {
      nav_requests: "リクエスト一覧",
      nav_billing: "請求・コスト見積もり",
      btn_sign_out: "ログアウト",
      auth_title: "ログイン",
      auth_subtitle: "デプロイ時のアクセスコードを入力して ImageUpscaler を開きます",
      auth_label: "アクセスコード",
      btn_unlock: "ロック解除",
      dash_title: "画像高画質化リクエスト",
      dash_subtitle: "CSVをアップロードし、バックグラウンド並列処理で画像を一括変換します。",
      btn_daily_log: "日次ログ (CSV) をダウンロード",
      btn_refresh: "更新",
      btn_raise_request: "+ 新規リクエスト作成",
      kpi_total_requests: "総リクエスト数",
      kpi_images_upscaled: "変換済み画像数",
      kpi_skipped_compliant: "スキップ (基準適合済)",
      kpi_est_spend: "累計推定コスト",
      kpi_engine_only: "エンジン",
      kpi_est_cost_single: "推定コスト",
      th_req_id: "リクエストID",
      th_file: "CSVファイル",
      th_engine: "エンジン",
      th_resolution: "解像度",
      th_status: "ステータス",
      th_progress: "画像数",
      th_cost: "推定コスト",
      th_created: "作成日時 (UTC)",
      th_actions: "操作",
      btn_back_requests: "← リクエスト一覧へ戻る",
      step_1_title: "CSV選択",
      step_2_title: "エンジン選択",
      step_3_title: "確認・送信",
      wiz_s1_heading: "画像URLを含むCSVをアップロード",
      wiz_s1_sub: "id,image_link または画像URL列を含むCSVファイルを選択してください。",
      dropzone_default_title: "クリックしてCSVファイルを選択",
      dropzone_default_sub: ".csv / .tsv / .txt (最大 10 MB)",
      btn_download_template: "サンプルCSVテンプレートをダウンロード",
      btn_continue: "次へ →",
      btn_back: "← 戻る",
      wiz_s2_heading: "エンジンと目標解像度の選択",
      wiz_s2_sub: "CSVに対する推定コストを比較し、最適なエンジンを選択してください。",
      wiz_select_res: "1. 目標解像度",
      wiz_select_engine: "2. エンジンの選択 (このCSVの推定コスト)",
      badge_recommended: "推奨",
      badge_minimum: "最小基準",
      res_1500_desc: "Google Merchant Center 推奨の高解像度ズーム対応サイズ。",
      res_500_desc: "500×500 px の最低要件を最速・最小コストで満たします。",
      eng_lc_desc: "ハルシネーション0%。文字・ロゴ・型番を含む商品画像に最適。",
      eng_esr_desc: "輪郭をシャープに補正する高速GPU超解像パイプライン。",
      eng_gfl_desc: "文字なしのアパレル・雑貨向けの低コスト生成AI高画質化。",
      eng_gf_desc: "1500×1500 px 向けに質感とディテールを高精度に補完。",
      eng_gp_desc: "主力商品（ヒーローSKU）向けの最高品質・高精度生成AI。",
      wiz_s3_heading: "内容確認・バックグラウンド実行",
      wiz_s3_sub: "送信すると即座にジョブ登録され、画面をブロックせずに並列処理されます。",
      rev_file: "CSVファイル名",
      rev_rows: "検出された画像行数",
      rev_engine: "選択エンジン",
      rev_res: "目標解像度",
      rev_cost: "推定コスト",
      btn_submit_job: "リクエストを送信",
      btn_dl_input: "入力CSVをダウンロード",
      btn_dl_output: "出力CSVをダウンロード",
      detail_items_heading: "このリクエストの画像変換結果",
      th_dimensions: "サイズ (元 → 変換後)",
      th_source_url: "元画像URL",
      th_converted_url: "変換後GCS URL",
      billing_title: "請求・コスト見積もり",
      billing_subtitle: "エンジン別の料金比較とリクエストごとのコスト内訳を確認できます。",
      billing_total_spend: "累計コスト合計",
      billing_total_imgs: "変換済み画像総数",
      billing_avg_cost: "1枚あたり平均コスト",
      calc_heading: "コストシミュレーター (最適エンジンの選択)",
      calc_sub: "画像枚数と解像度を入力すると5つのエンジンの費用と目安時間を即座に比較します。",
      calc_num_images: "画像枚数",
      calc_col_rate: "10,000枚あたり単価",
      calc_col_est: "推定コスト合計",
      calc_col_time: "推定処理時間",
      calc_col_best: "おすすめの用途",
      billing_history_heading: "リクエスト別コスト内訳",
      btn_open_row: "詳細 →",
      btn_dl_csv_short: "CSV出力",
      btn_use_engine: "このエンジンを選択 →",
      kpi_failed_images: "エラー / 失敗",
      failed_skus_heading: "エラーが発生したSKU一覧 (クリックで詳細・再試行):",
      btn_show_all: "すべて表示",
      lbl_rows_per_page: "表示件数:",
      progress_live: "並列処理の進捗状況",
      progress_completed: "処理完了",
      filter_all: "すべて",
      filter_converted: "変換済",
      filter_skipped: "スキップ",
      filter_failed: "失敗",
      filter_processing: "処理中",
      btn_retry_all_failed: "↻ 失敗したSKUを一括再試行",
      detail_click_sku_hint:
        "任意のSKU行をクリックすると、個別プレビュー・エラー原因の確認・再試行（請求額の自動更新）が行えます。",
      btn_sync_gcp_pricing: "↻ GCP最新料金と同期",
      sku_modal_eyebrow: "SKU 詳細・再試行",
      sku_modal_err_heading: "失敗原因の診断結果:",
      sku_modal_latency: "処理時間",
      sku_modal_billing: "このSKUの課金額",
      sku_modal_converted_label: "変換後GCS画像",
      sku_modal_retry_engine_lbl: "再試行エンジン:",
      btn_close: "閉じる",
      btn_retry_sku: "↻ このSKUを再試行 (請求額を自動更新)",
      eta_remaining_fmt: "⏱ 残り約 {eta} · 経過 {elapsed}",
      eta_completed_fmt: "✓ 完了時間: {elapsed}",
      loading_requests: "クラウドDBからリクエスト履歴を読み込んでいます...",
      loading_sku_items: "SKU変換データを読み込んでいます...",
      loading_billing_history: "請求・コスト履歴を読み込んでいます...",
      loading_signing_in: "認証中・ワークスペースを読み込んでいます...",
      loading_refreshing: "更新中...",
      loading_submitting: "ジョブを登録中...",
      loading_retrying_sku: "再試行中 (画像を変換・請求額を更新)...",
      loading_retrying_all: "失敗SKUを一括再試行中...",
      loading_syncing_pricing: "GCP料金と同期中...",
      loading_downloading: "ダウンロード中...",
      loading_parsing_csv: "CSVファイルを解析中...",
      sku_comp_title: "ビフォー・アフター比較スライダー (元画像 vs 変換後)",
      sku_comp_hint: "スライダーを左右にドラッグして、元画像（左）と高画質化後の画像（右）を比較できます",
      sku_comp_before_lbl: "← 元画像 (Before)",
      sku_comp_after_lbl: "変換後 (After) →",
    },
    en: {
      nav_requests: "Requests",
      nav_billing: "Billing & Cost Estimator",
      btn_sign_out: "Sign out",
      auth_title: "Sign in",
      auth_subtitle:
        "Enter your deployment access code to continue to ImageUpscaler",
      auth_label: "Deployment Access Code",
      btn_unlock: "Unlock Workspace",
      dash_title: "Upscaling Requests",
      dash_subtitle:
        "Upload product image CSVs, run parallel background jobs, and " +
        "download converted outputs.",
      btn_daily_log: "Download Daily Log (CSV)",
      btn_refresh: "Refresh",
      btn_raise_request: "+ Raise New Request",
      kpi_total_requests: "Total Requests",
      kpi_images_upscaled: "Images Upscaled",
      kpi_skipped_compliant: "Skipped (Already Compliant)",
      kpi_est_spend: "Total Estimated Cost",
      kpi_engine_only: "Engine",
      kpi_est_cost_single: "Estimated Cost",
      th_req_id: "Request ID",
      th_file: "CSV File",
      th_engine: "Engine",
      th_resolution: "Resolution",
      th_status: "Status",
      th_progress: "Images",
      th_cost: "Est. Cost",
      th_created: "Created (UTC)",
      th_actions: "Actions",
      btn_back_requests: "← Back to Requests",
      step_1_title: "Upload CSV",
      step_2_title: "Choose Engine",
      step_3_title: "Review & Submit",
      wiz_s1_heading: "Upload your CSV file with Image URLs",
      wiz_s1_sub:
        "Upload a CSV or TSV file containing product image links (e.g., " +
        "id,image_link,additional_image_link or url).",
      dropzone_default_title: "Click to select a CSV file",
      dropzone_default_sub: "Supports .csv, .tsv, or .txt up to 10 MB",
      btn_download_template: "Download sample template",
      btn_continue: "Continue →",
      btn_back: "← Back",
      wiz_s2_heading: "Choose Upscaling Engine & Resolution",
      wiz_s2_sub:
        "Compare estimated costs for your CSV below and pick the best engine " +
        "for your catalog.",
      wiz_select_res: "1. Target Resolution",
      wiz_select_engine:
        "2. Select Processing Engine (Estimated Cost for Your CSV)",
      badge_recommended: "Recommended",
      badge_minimum: "Minimum Policy",
      res_1500_desc:
        "Full high-resolution zoom compliance for Google Merchant Center.",
      res_500_desc:
        "Meets baseline 500×500 px minimum threshold with lowest compute cost.",
      eng_lc_desc:
        "0% hallucination. Deterministic Lanczos + white canvas, ideal for " +
        "text & logos.",
      eng_esr_desc:
        "High-throughput GPU edge sharpening for compressed product " +
        "thumbnails.",
      eng_gfl_desc:
        "Low-cost generative super-resolution for text-free lifestyle & " +
        "apparel photos.",
      eng_gf_desc:
        "Balanced generative texture synthesis for 1500×1500 px marketplace " +
        "compliance.",
      eng_gp_desc:
        "Maximum visual fidelity for flagship hero SKUs requiring deep " +
        "detail recovery.",
      wiz_s3_heading: "Review & Launch Background Job",
      wiz_s3_sub:
        "Your job will be queued immediately and processed in parallel in " +
        "the background.",
      rev_file: "CSV Filename",
      rev_rows: "Detected Image Rows",
      rev_engine: "Selected Engine",
      rev_res: "Target Resolution",
      rev_cost: "Estimated Cost",
      btn_submit_job: "Submit Upscaling Request",
      btn_dl_input: "Download Input CSV",
      btn_dl_output: "Download Output CSV",
      detail_items_heading: "Converted Images in this Request",
      th_dimensions: "Dimensions",
      th_source_url: "Source URL",
      th_converted_url: "Converted GCS URL",
      billing_title: "Billing & Cost Estimator",
      billing_subtitle:
        "Compare engine pricing before raising a request and track cost per " +
        "completed job.",
      billing_total_spend: "Total Cumulative Cost",
      billing_total_imgs: "Total Images Upscaled",
      billing_avg_cost: "Average Cost / Image",
      calc_heading: "Interactive Cost Calculator — Choose the Best Method",
      calc_sub:
        "Enter your expected image count and target resolution to compare " +
        "all 5 engines.",
      calc_num_images: "Number of Images",
      calc_col_rate: "Rate / 10,000 Images",
      calc_col_est: "Estimated Total Cost",
      calc_col_time: "Est. Processing Time",
      calc_col_best: "Best For",
      billing_history_heading: "Cost Breakdown per Request",
      btn_open_row: "Open →",
      btn_dl_csv_short: "Download CSV",
      btn_use_engine: "Use this Engine →",
      kpi_failed_images: "Failed / Errors",
      failed_skus_heading: "Failed SKUs (Click to inspect & retry):",
      btn_show_all: "Show All",
      lbl_rows_per_page: "Rows per page:",
      progress_live: "Live Parallel Progress",
      progress_completed: "Processing Completed",
      filter_all: "All",
      filter_converted: "Converted",
      filter_skipped: "Skipped",
      filter_failed: "Failed",
      filter_processing: "Processing",
      btn_retry_all_failed: "↻ Retry All Failed SKUs",
      detail_click_sku_hint:
        "Click any SKU row to inspect before/after previews, view failure " +
        "diagnostics, or retry the SKU (billing updates automatically).",
      btn_sync_gcp_pricing: "↻ Sync with GCP Pricing",
      sku_modal_eyebrow: "SKU Inspection & Retry",
      sku_modal_err_heading: "Failure Diagnostic Reason:",
      sku_modal_latency: "Processing Time",
      sku_modal_billing: "SKU Billing Cost",
      sku_modal_converted_label: "Converted GCS Image",
      sku_modal_retry_engine_lbl: "Retry Engine:",
      btn_close: "Close",
      btn_retry_sku: "↻ Retry SKU & Update Billing",
      eta_remaining_fmt: "⏱ Est. ~{eta} remaining · Elapsed {elapsed}",
      eta_completed_fmt: "✓ Completed in {elapsed}",
      loading_requests: "Loading upscaling requests from Cloud Firestore...",
      loading_sku_items: "Loading SKU conversion records...",
      loading_billing_history: "Loading request cost history...",
      loading_signing_in: "Signing in & loading workspace...",
      loading_refreshing: "Refreshing...",
      loading_submitting: "Submitting job...",
      loading_retrying_sku: "Retrying SKU & updating billing...",
      loading_retrying_all: "Retrying all failed SKUs...",
      loading_syncing_pricing: "Syncing GCP pricing...",
      loading_downloading: "Downloading...",
      loading_parsing_csv: "Parsing CSV file...",
      sku_comp_title: "Interactive Before / After Image Comparator",
      sku_comp_hint:
        "Drag the slider left or right to compare the original SKU image " +
        "against the upscaled output",
      sku_comp_before_lbl: "← Source (Before)",
      sku_comp_after_lbl: "Upscaled (After) →",
    },
    "zh-CN": {
      nav_requests: "任务列表",
      nav_billing: "计费与成本估算",
      btn_sign_out: "退出登录",
      auth_title: "登录",
      auth_subtitle: "请输入部署访问代码以继续使用 ImageUpscaler",
      auth_label: "部署访问代码",
      btn_unlock: "解锁工作区",
      dash_title: "图片超分辨率请求",
      dash_subtitle: "上传商品图片 CSV，后台并行处理并下载转换后的结果文件。",
      btn_daily_log: "下载每日日志 (CSV)",
      btn_refresh: "刷新",
      btn_raise_request: "+ 创建新请求",
      kpi_total_requests: "总请求数",
      kpi_images_upscaled: "已放大图片数",
      kpi_skipped_compliant: "已跳过 (符合尺寸)",
      kpi_est_spend: "累计预估成本",
      kpi_engine_only: "处理引擎",
      kpi_est_cost_single: "预估成本",
      th_req_id: "任务 ID",
      th_file: "CSV 文件",
      th_engine: "引擎",
      th_resolution: "目标分辨率",
      th_status: "状态",
      th_progress: "图片进度",
      th_cost: "预估费用",
      th_created: "创建时间 (UTC)",
      th_actions: "操作",
      btn_back_requests: "← 返回任务列表",
      step_1_title: "上传 CSV",
      step_2_title: "选择引擎",
      step_3_title: "确认并提交",
      wiz_s1_heading: "上传包含图片链接的 CSV 文件",
      wiz_s1_sub: "支持包含 id,image_link 或图片 URL 列的 CSV / TSV 文件。",
      dropzone_default_title: "点击选择 CSV 文件",
      dropzone_default_sub: "支持 .csv, .tsv, .txt (最大 10 MB)",
      btn_download_template: "下载示例 CSV 模板",
      btn_continue: "下一步 →",
      btn_back: "← 上一步",
      wiz_s2_heading: "选择超分引擎与目标分辨率",
      wiz_s2_sub: "对比下方各引擎的预估费用，为您的商品目录选择最佳方案。",
      wiz_select_res: "1. 目标分辨率",
      wiz_select_engine: "2. 选择处理引擎 (当前 CSV 预估费用)",
      badge_recommended: "推荐",
      badge_minimum: "最低合规标准",
      res_1500_desc: "符合 Google Merchant Center 高清缩放展示标准。",
      res_500_desc: "以最快速度与最低成本满足 500×500 px 基础合规要求。",
      eng_lc_desc: "0% 幻觉失真。保留商品包装文字、Logo 与型号，性价比最高。",
      eng_esr_desc: "高速 GPU 边缘锐化与超分辨率流水线。",
      eng_gfl_desc: "适用于无文字服饰与家居图片的低成本生成式 AI 放大。",
      eng_gf_desc: "针对 1500×1500 px 提供均衡的纹理细节合成。",
      eng_gp_desc: "适用于核心主打商品 (Hero SKU) 的最高画质生成式 AI。",
      wiz_s3_heading: "确认信息并启动后台并行任务",
      wiz_s3_sub: "提交后任务将立即进入后台并行处理，不会阻塞您的页面。",
      rev_file: "CSV 文件名",
      rev_rows: "检测到的图片行数",
      rev_engine: "所选引擎",
      rev_res: "目标分辨率",
      rev_cost: "预估费用",
      btn_submit_job: "提交超分任务",
      btn_dl_input: "下载输入 CSV",
      btn_dl_output: "下载输出 CSV",
      detail_items_heading: "该任务的图片转换明细",
      th_dimensions: "尺寸 (原图 → 输出)",
      th_source_url: "原图链接",
      th_converted_url: "转换后 GCS 链接",
      billing_title: "计费与成本计算器",
      billing_subtitle: "在提交任务前对比 5 种引擎的价格与耗时，并查看各任务费用明细。",
      billing_total_spend: "累计总费用",
      billing_total_imgs: "已处理图片总数",
      billing_avg_cost: "单张图片平均成本",
      calc_heading: "交互式成本计算器 — 选择最佳方案",
      calc_sub: "输入预计图片数量与目标分辨率，立即对比所有引擎的费用与处理时间。",
      calc_num_images: "图片数量",
      calc_col_rate: "每万张单价",
      calc_col_est: "预估总费用",
      calc_col_time: "预计耗时",
      calc_col_best: "适用场景",
      billing_history_heading: "各任务费用明细",
      btn_open_row: "查看 →",
      btn_dl_csv_short: "下载 CSV",
      btn_use_engine: "使用此引擎 →",
      kpi_failed_images: "失败 / 错误",
      failed_skus_heading: "失败的 SKU 列表 (点击筛选):",
      btn_show_all: "显示全部",
      lbl_rows_per_page: "每页行数:",
      progress_live: "并行处理实时进度",
      progress_completed: "处理已完成",
      filter_all: "全部",
      filter_converted: "已转换",
      filter_skipped: "已跳过",
      filter_failed: "失败",
      filter_processing: "处理中",
    },
    "zh-TW": {
      nav_requests: "任務列表",
      nav_billing: "計費與成本估算",
      btn_sign_out: "登出",
      auth_title: "登入",
      auth_subtitle: "請輸入部署存取碼以繼續使用 ImageUpscaler",
      auth_label: "部署存取碼",
      btn_unlock: "解鎖工作區",
      dash_title: "圖片高畫質化請求",
      dash_subtitle: "上傳商品圖片 CSV，背景平行處理並下載轉換結果。",
      btn_daily_log: "下載每日日誌 (CSV)",
      btn_refresh: "重新整理",
      btn_raise_request: "+ 建立新請求",
      kpi_total_requests: "總請求數",
      kpi_images_upscaled: "已放大圖片數",
      kpi_skipped_compliant: "已略過 (符合尺寸)",
      kpi_est_spend: "累計預估成本",
      kpi_engine_only: "處理引擎",
      kpi_est_cost_single: "預估成本",
      btn_back_requests: "← 返回任務列表",
      step_1_title: "上傳 CSV",
      step_2_title: "選擇引擎",
      step_3_title: "確認並送出",
      btn_download_template: "下載範例 CSV 範本",
      btn_continue: "下一步 →",
      btn_back: "← 上一步",
      btn_submit_job: "送出任務",
      billing_title: "計費與成本計算器",
      btn_open_row: "檢視 →",
      btn_dl_csv_short: "下載 CSV",
      btn_use_engine: "使用此引擎 →",
      kpi_failed_images: "失敗 / 錯誤",
      failed_skus_heading: "失敗的 SKU 列表 (點擊篩選):",
      btn_show_all: "顯示全部",
      lbl_rows_per_page: "每頁列數:",
      progress_live: "平行處理即時進度",
      progress_completed: "處理已完成",
      filter_all: "全部",
      filter_converted: "已轉換",
      filter_skipped: "已略過",
      filter_failed: "失敗",
      filter_processing: "處理中",
    },
    ko: {
      nav_requests: "요청 목록",
      nav_billing: "요금 및 비용 계산기",
      btn_sign_out: "로그아웃",
      auth_title: "로그인",
      auth_subtitle: "ImageUpscaler를 계속하려면 배포 액세스 코드를 입력하세요",
      auth_label: "배포 액세스 코드",
      btn_unlock: "잠금 해제",
      dash_title: "이미지 업스케일링 요청",
      dash_subtitle: "상품 이미지 CSV를 업로드하고 백그라운드 병렬 처리로 고해상도 이미지를 생성합니다.",
      btn_daily_log: "일일 로그 (CSV) 다운로드",
      btn_refresh: "새로고침",
      btn_raise_request: "+ 새 요청 만들기",
      kpi_total_requests: "총 요청 수",
      kpi_images_upscaled: "업스케일 완료 이미지",
      kpi_skipped_compliant: "건너뜀 (규격 충족)",
      kpi_est_spend: "총 예상 비용",
      kpi_engine_only: "엔진",
      kpi_est_cost_single: "예상 비용",
      th_req_id: "요청 ID",
      th_file: "CSV 파일",
      th_engine: "엔진",
      th_resolution: "해상도",
      th_status: "상태",
      th_progress: "이미지 수",
      th_cost: "예상 비용",
      th_created: "생성 시간 (UTC)",
      th_actions: "작업",
      btn_back_requests: "← 요청 목록으로 돌아가기",
      step_1_title: "CSV 업로드",
      step_2_title: "엔진 선택",
      step_3_title: "검토 및 제출",
      wiz_s1_heading: "이미지 URL이 포함된 CSV 파일 업로드",
      wiz_s1_sub: "id,image_link 또는 이미지 URL 열이 포함된 CSV/TSV 파일을 선택하세요.",
      dropzone_default_title: "클릭하여 CSV 파일 선택",
      dropzone_default_sub: ".csv, .tsv, .txt 지원 (최대 10 MB)",
      btn_download_template: "샘플 CSV 템플릿 다운로드",
      btn_continue: "다음 →",
      btn_back: "← 이전",
      wiz_s2_heading: "업스케일링 엔진 및 목표 해상도 선택",
      wiz_s2_sub: "CSV에 대한 예상 비용을 비교하고 최적의 엔진을 선택하세요.",
      wiz_select_res: "1. 목표 해상도",
      wiz_select_engine: "2. 처리 엔진 선택 (현재 CSV 예상 비용)",
      badge_recommended: "권장",
      badge_minimum: "최소 규격",
      res_1500_desc: "Google 판매자 센터 고해상도 줌 권장 규격 충족.",
      res_500_desc: "최저 비용 및 최고 속도로 500×500 px 최소 규격 충족.",
      eng_lc_desc: "할루시네이션 0%. 텍스트, 로고, 라벨이 있는 상품 이미지에 최적.",
      eng_esr_desc: "압축된 썸네일 윤곽선을 선명하게 보정하는 고속 GPU 파이프라인.",
      eng_gfl_desc: "텍스트가 없는 의류/잡화 이미지를 위한 저비용 생성형 AI.",
      eng_gf_desc: "1500×1500 px 해상도를 위한 균형 잡힌 생성형 디테일 복원.",
      eng_gp_desc: "핵심 대표 상품(Hero SKU)을 위한 최고 품질 생성형 AI.",
      wiz_s3_heading: "검토 및 백그라운드 작업 실행",
      wiz_s3_sub: "제출 즉시 백그라운드에서 병렬 처리되며 화면이 차단되지 않습니다.",
      rev_file: "CSV 파일명",
      rev_rows: "감지된 이미지 행 수",
      rev_engine: "선택한 엔진",
      rev_res: "목표 해상도",
      rev_cost: "예상 비용",
      btn_submit_job: "업스케일링 요청 제출",
      btn_dl_input: "입력 CSV 다운로드",
      btn_dl_output: "출력 CSV 다운로드",
      detail_items_heading: "이 요청의 변환된 이미지 목록",
      th_dimensions: "크기 (원본 → 변환 후)",
      th_source_url: "원본 이미지 URL",
      th_converted_url: "변환된 GCS URL",
      billing_title: "요금 및 비용 계산기",
      billing_subtitle: "요청 전 5개 엔진의 예상 비용과 처리 시간을 비교하고 요청별 비용을 확인하세요.",
      billing_total_spend: "누적 총 비용",
      billing_total_imgs: "총 변환 이미지 수",
      billing_avg_cost: "이미지당 평균 비용",
      calc_heading: "비용 시뮬레이터 — 최적의 엔진 선택",
      calc_sub: "이미지 수량과 해상도를 입력하여 5개 엔진의 비용과 예상 시간을 비교하세요.",
      calc_num_images: "이미지 수량",
      calc_col_rate: "10,000장당 단가",
      calc_col_est: "예상 총 비용",
      calc_col_time: "예상 소요 시간",
      calc_col_best: "추천 용도",
      billing_history_heading: "요청별 비용 내역",
      btn_open_row: "열기 →",
      btn_dl_csv_short: "CSV 다운로드",
      btn_use_engine: "이 엔진 선택 →",
      kpi_failed_images: "실패 / 오류",
      failed_skus_heading: "오류가 발생한 SKU 목록 (클릭하여 필터링):",
      btn_show_all: "전체 보기",
      lbl_rows_per_page: "페이지당 행 수:",
      progress_live: "병렬 처리 진행 상황",
      progress_completed: "처리 완료",
      filter_all: "전체",
      filter_converted: "변환됨",
      filter_skipped: "건너뜀",
      filter_failed: "실패",
      filter_processing: "처리 중",
    },
    vi: {
      nav_requests: "Danh sách yêu cầu",
      nav_billing: "Chi phí & Ước tính",
      btn_sign_out: "Đăng xuất",
      auth_title: "Đăng nhập",
      auth_subtitle: "Nhập mã truy cập triển khai để tiếp tục",
      btn_unlock: "Mở khóa",
      dash_title: "Yêu cầu nâng cấp ảnh",
      btn_raise_request: "+ Tạo yêu cầu mới",
      kpi_engine_only: "Công cụ (Engine)",
      btn_submit_job: "Gửi yêu cầu",
      billing_title: "Bảng tính chi phí & Thanh toán",
      kpi_failed_images: "Lỗi / Thất bại",
      failed_skus_heading: "Danh sách SKU lỗi (Nhấp để lọc):",
      btn_show_all: "Hiển thị tất cả",
      lbl_rows_per_page: "Số hàng mỗi trang:",
      progress_live: "Tiến độ xử lý song song",
      progress_completed: "Đã hoàn tất xử lý",
      filter_all: "Tất cả",
      filter_converted: "Đã chuyển đổi",
      filter_skipped: "Đã bỏ qua",
      filter_failed: "Lỗi",
      filter_processing: "Đang xử lý",
    },
    th: {
      nav_requests: "รายการคำขอ",
      nav_billing: "การเรียกเก็บเงินและคำนวณต้นทุน",
      btn_sign_out: "ออกจากระบบ",
      auth_title: "เข้าสู่ระบบ",
      auth_subtitle: "ป้อนรหัสการเข้าถึงเพื่อใช้งาน ImageUpscaler",
      btn_unlock: "ปลดล็อก",
      dash_title: "คำขอเพิ่มความละเอียดรูปภาพ",
      btn_raise_request: "+ สร้างคำขอใหม่",
      kpi_engine_only: "เอนจิน",
      btn_submit_job: "ส่งคำขอ",
      billing_title: "คำนวณต้นทุนและค่าใช้จ่าย",
      kpi_failed_images: "ล้มเหลว / ข้อผิดพลาด",
      failed_skus_heading: "รายการ SKU ที่ล้มเหลว (คลิกเพื่อกรอง):",
      btn_show_all: "แสดงทั้งหมด",
      lbl_rows_per_page: "จำนวนแถวต่อหน้า:",
      progress_live: "ความคืบหน้าการประมวลผลแบบขนาน",
      progress_completed: "ประมวลผลเสร็จสิ้น",
      filter_all: "ทั้งหมด",
      filter_converted: "แปลงแล้ว",
      filter_skipped: "ข้าม",
      filter_failed: "ล้มเหลว",
      filter_processing: "กำลังประมวลผล",
    },
    id: {
      nav_requests: "Daftar Permintaan",
      nav_billing: "Tagihan & Estimasi Biaya",
      btn_sign_out: "Keluar",
      auth_title: "Masuk",
      auth_subtitle: "Masukkan kode akses deployment untuk melanjutkan",
      btn_unlock: "Buka Kunci",
      dash_title: "Permintaan Upscale Gambar",
      btn_raise_request: "+ Buat Permintaan Baru",
      kpi_engine_only: "Mesin (Engine)",
      btn_submit_job: "Kirim Permintaan",
      billing_title: "Kalkulator Biaya & Tagihan",
      kpi_failed_images: "Gagal / Error",
      failed_skus_heading: "Daftar SKU Gagal (Klik untuk memfilter):",
      btn_show_all: "Tampilkan Semua",
      lbl_rows_per_page: "Baris per halaman:",
      progress_live: "Progres Pemrosesan Paralel", // NOTYPO
      progress_completed: "Pemrosesan Selesai",
      filter_all: "Semua",
      filter_converted: "Dikonversi",
      filter_skipped: "Dilewati",
      filter_failed: "Gagal",
      filter_processing: "Memproses",
    },
    hi: {
      nav_requests: "सभी रिक्वेस्ट",
      nav_billing: "बिलिंग और लागत अनुमान",
      btn_sign_out: "साइन आउट",
      auth_title: "साइन इन करें",
      auth_subtitle: "ImageUpscaler खोलने के लिए एक्सेस कोड दर्ज करें",
      auth_label: "डिप्लॉयमेंट एक्सेस कोड",
      btn_unlock: "अनलॉक करें",
      dash_title: "इमेज अपस्केलिंग रिक्वेस्ट",
      dash_subtitle:
        "CSV अपलोड करें, बैकग्राउंड में समानांतर प्रोसेसिंग चलाएं और आउटपुट " +
        "डाउनलोड करें।",
      btn_daily_log: "डेली लॉग (CSV) डाउनलोड",
      btn_refresh: "रीफ्रेश",
      btn_raise_request: "+ नई रिक्वेस्ट बनाएं",
      kpi_total_requests: "कुल रिक्वेस्ट",
      kpi_images_upscaled: "अपस्केल की गई इमेज",
      kpi_skipped_compliant: "स्किप की गई इमेज",
      kpi_est_spend: "कुल अनुमानित लागत",
      kpi_engine_only: "इंजन",
      kpi_est_cost_single: "अनुमानित लागत",
      btn_back_requests: "← वापस जाएं",
      btn_download_template: "सैंपल CSV टेम्पलेट डाउनलोड करें",
      btn_submit_job: "रिक्वेस्ट सबमिट करें",
      billing_title: "बिलिंग और लागत कैलकुलेटर",
      kpi_failed_images: "त्रुटि / विफल",
      failed_skus_heading: "विफल SKU सूची (फ़िल्टर करने के लिए क्लिक करें):",
      btn_show_all: "सभी दिखाएं",
      lbl_rows_per_page: "प्रति पृष्ठ पंक्तियाँ:",
      progress_live: "समानांतर प्रोसेसिंग प्रगति",
      progress_completed: "प्रोसेसिंग पूर्ण",
      filter_all: "सभी",
      filter_converted: "कन्वर्टेड",
      filter_skipped: "स्किप",
      filter_failed: "विफल",
      filter_processing: "प्रोसेसिंग",
    },
  };

  const topProgressBar = document.getElementById("top-progress-bar");
  const statusBanner = document.getElementById("status-banner");
  const navTabs = document.getElementById("nav-tabs");
  const navTabRequests = document.getElementById("nav-tab-requests");
  const navTabBilling = document.getElementById("nav-tab-billing");
  const btnLogout = document.getElementById("btn-logout");
  const selectLanguage = document.getElementById("select-language");

  const viewAuth = document.getElementById("view-auth");
  const authenticatedShell = document.getElementById("authenticated-shell");
  const viewDashboard = document.getElementById("view-dashboard");
  const viewWizard = document.getElementById("view-wizard");
  const viewRequestDetail = document.getElementById("view-request-detail");
  const viewBilling = document.getElementById("view-billing");

  const inputAccessCode = document.getElementById("input-access-code");
  const requestsTableTbody = document.getElementById("requests-table-tbody");

  // Wizard elements
  const fileUpload = document.getElementById("file-upload");
  const dropzoneArea = document.getElementById("dropzone-area");
  const dropzoneTitle = document.getElementById("dropzone-title");
  const dropzoneSubtitle = document.getElementById("dropzone-subtitle");
  const btnStep1Next = document.getElementById("btn-step1-next");
  const btnSubmitRequest = document.getElementById("btn-submit-request");

  const LANG_STORAGE_KEY = "gmc_ui_lang";

  function getSavedLanguage() {
    try {
      const saved = window.localStorage.getItem(LANG_STORAGE_KEY);
      if (saved && Object.prototype.hasOwnProperty.call(I18N, saved)) {
        return saved;
      }
    } catch (e) {
      // Ignore storage access errors in restricted browser contexts
    }
    return "ja";
  }

  function saveLanguagePreference(lang) {
    try {
      window.localStorage.setItem(LANG_STORAGE_KEY, lang);
    } catch (e) {
      // Ignore storage access errors in restricted browser contexts
    }
  }

  function t(key) {
    const dict = I18N[currentLang] || I18N.ja;
    return dict[key] || I18N.ja[key] || I18N.en[key] || key;
  }

  function applyLanguage(lang) {
    currentLang =
      Object.prototype.hasOwnProperty.call(I18N, lang) ? lang : "ja";
    saveLanguagePreference(currentLang);
    document.documentElement.lang = currentLang;
    if (selectLanguage.value !== currentLang) {
      selectLanguage.value = currentLang;
    }
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      const key = el.getAttribute("data-i18n");
      if (key) {
        el.textContent = t(key);
      }
    });
    if (!selectedFilename) {
      dropzoneTitle.textContent = t("dropzone_default_title");
      dropzoneSubtitle.textContent = t("dropzone_default_sub");
    }
    renderBillingCalculator();
    if (cachedRequests.length) {
      renderRequestsTable(cachedRequests);
    }
    if (detailCurrentRequest) {
      renderRequestDetail({ request: detailCurrentRequest,
        items: detailItemsCache });
    }
  }

  function formatUsd(val) {
    const num = Number(val || 0);
    if (num === 0) {
      return "$0.0000";
    }
    if (num < 0.01) {
      return "$" + num.toFixed(6);
    }
    return "$" + num.toFixed(4);
  }

  function formatDurationEstimate(numImages, imgsPerSec) {
    const secs =
      Math.max(1, Math.ceil(Number(numImages || 1) / Number(imgsPerSec ||
        100)));
    if (secs < 60) {
      return "~" + secs + " sec";
    }
    const mins = (secs / 60).toFixed(1);
    return "~" + mins + " min (" + secs + "s)";
  }

  function estimateJobCost(engineId, targetRes, numImages) {
    const spec = ENGINE_COST_MAP[engineId] || ENGINE_SPECS[0];
    const unit = Number(targetRes) === 500 ? spec.cost500 : spec.cost1500;
    return Math.max(0, Number(numImages || 0)) * unit;
  }

  function setLoading(isLoading) {
    if (isLoading) {
      topProgressBar.classList.add("loading");
    } else {
      topProgressBar.classList.remove("loading");
    }
  }

  function setButtonLoading(btnEl, isLoading, loadingLabel) {
    if (!btnEl) {
      return;
    }
    if (isLoading) {
      if (!btnEl.getAttribute("data-orig-text")) {
        btnEl.setAttribute("data-orig-text", btnEl.textContent.trim());
      }
      btnEl.disabled = true;
      const spinner = document.createElement("span");
      spinner.className = "btn-spinner";
      spinner.setAttribute("aria-hidden", "true");
      const textNode = document.createElement("span");
      textNode.textContent = loadingLabel || t("loading_refreshing");
      btnEl.replaceChildren(spinner, textNode);
    } else {
      btnEl.disabled = false;
      const i18nKey = btnEl.getAttribute("data-i18n");
      const fallbackText = btnEl.getAttribute("data-orig-text") || "";
      btnEl.replaceChildren(document.createTextNode(i18nKey ? t(i18nKey)
        : fallbackText));
    }
  }

  function setKpisLoading(kpiIds, isLoading) {
    (kpiIds || []).forEach(function (id) {
      const el = document.getElementById(id);
      if (!el) {
        return;
      }
      if (isLoading) {
        el.classList.add("kpi-loading-shimmer");
      } else {
        el.classList.remove("kpi-loading-shimmer");
      }
    });
  }

  function renderTableLoader(tbodyEl, colSpan, messageText, skeletonRowCount) {
    if (!tbodyEl) {
      return;
    }
    const rows = [];
    const bannerTr = document.createElement("tr");
    const bannerTd = document.createElement("td");
    bannerTd.setAttribute("colspan", String(colSpan || 5));
    const bannerWrap = document.createElement("div");
    bannerWrap.className = "table-loader-banner";
    const ring = document.createElement("span");
    ring.className = "google-spinner-ring";
    ring.setAttribute("aria-hidden", "true");
    const msgSpan = document.createElement("span");
    msgSpan.textContent = messageText || t("loading_requests");
    bannerWrap.appendChild(ring);
    bannerWrap.appendChild(msgSpan);
    bannerTd.appendChild(bannerWrap);
    bannerTr.appendChild(bannerTd);
    rows.push(bannerTr);

    const widths =
      ["w-60", "w-75", "w-90", "w-40", "w-75", "w-60", "w-40", "w-75", "w-60"];
    const numRows = Math.max(2, Number(skeletonRowCount || 4));
    for (let r = 0; r < numRows; r++) {
      const skTr = document.createElement("tr");
      for (let c = 0; c < colSpan; c++) {
        const skTd = document.createElement("td");
        const bar = document.createElement("span");
        bar.className = "skeleton-bar " + widths[(r + c) % widths.length];
        skTd.appendChild(bar);
        skTr.appendChild(skTd);
      }
      rows.push(skTr);
    }
    tbodyEl.replaceChildren(...rows);
  }

  function showToast(message, variant) {
    if (!message) {
      statusBanner.classList.add("hidden");
      return;
    }
    statusBanner.className = "toast-banner toast-" + (variant || "info");
    statusBanner.textContent = message;
  }

  function buildAuthHeaders(extraHeaders) {
    const headers = Object.assign({}, extraHeaders || {});
    if (csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }
    if (activeAccessCode) {
      headers["X-Access-Code"] = activeAccessCode;
    }
    return headers;
  }

  function createCell(text, className) {
    const td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    if (className) {
      td.className = className;
    }
    return td;
  }

  function createStatusChip(statusText) {
    const raw = String(statusText || "PROCESSING");
    const chip = document.createElement("span");
    let cls = "chip-amber";
    let label = raw;
    if (raw === "COMPLETED" || raw === "CONVERTED") {
      cls = "chip-green";
    } else if (raw === "SKIPPED_COMPLIANT") {
      cls = "chip-blue";
    } else if (raw.indexOf("FAILED") === 0) {
      cls = "chip-red";
      label = "FAILED";
    } else if (raw === "PROCESSING" || raw === "QUEUED") {
      cls = "chip-amber";
    }
    chip.className = "chip " + cls;
    chip.textContent = label;
    return chip;
  }

  function extractItemStatusAndError(item, requestStatus) {
    const rawStatus = String((item && item.status) || "");
    const isReqProcessing =
      requestStatus === "PROCESSING" || requestStatus === "QUEUED";

    if (rawStatus === "CONVERTED" || (item && item.converted_gcs_url)) {
      return { status: "CONVERTED", errorReason: "", errorType: "" };
    }
    if (rawStatus === "SKIPPED_COMPLIANT") {
      return { status: "SKIPPED_COMPLIANT", errorReason: "", errorType: "" };
    }
    if (rawStatus === "PROCESSING" || rawStatus === "QUEUED") {
      return { status: "PROCESSING", errorReason: "", errorType: "" };
    }
    if (isReqProcessing && !(item && item.error_message) &&
      rawStatus.indexOf("FAILED (") === -1) {
      return { status: "PROCESSING", errorReason: "", errorType: "" };
    }

    let errReason = String((item && item.error_message) || "").trim();
    if (!errReason && rawStatus.indexOf("FAILED") === 0) {
      const openIdx = rawStatus.indexOf("(");
      const closeIdx = rawStatus.lastIndexOf(")");
      if (openIdx !== -1 && closeIdx > openIdx) {
        errReason = rawStatus.slice(openIdx + 1, closeIdx).trim();
      }
    }
    if (!errReason || errReason === "ReadError:" || errReason === "ReadError") {
      errReason =
        "ReadError: Remote CDN connection reset or timed out while " +
        "downloading image";
    }

    let errType = "Error";
    const colonIdx = errReason.indexOf(":");
    if (colonIdx > 0 && colonIdx <= 28) {
      errType = errReason.slice(0, colonIdx).trim();
    } else if (errReason.indexOf("HTTP ") === 0) {
      errType = errReason.slice(0, 8).trim();
    }

    return {
      status: "FAILED",
      errorReason: errReason,
      errorType: errType,
    };
  }

  function renderPaginationControls(infoEl, btnGroupEl, totalItems, currentPage,
    pageSize, onPageSelect) {
    if (!infoEl || !btnGroupEl) {
      return;
    }
    const total = Math.max(0, Number(totalItems || 0));
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const safePage = Math.min(Math.max(1, currentPage), totalPages);

    if (total === 0) {
      infoEl.textContent = "Showing 0 of 0";
      btnGroupEl.replaceChildren();
      return;
    }

    const startIdx = (safePage - 1) * pageSize + 1;
    const endIdx = Math.min(total, safePage * pageSize);
    infoEl.textContent = "Showing " + startIdx + "–" + endIdx + " of " + total;

    const buttons = [];

    function makeBtn(label, targetPage, disabled, isActive) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "page-btn" + (isActive ? " active" : "");
      btn.disabled = Boolean(disabled);
      btn.textContent = String(label);
      if (!disabled && !isActive) {
        btn.addEventListener("click", function () {
          onPageSelect(targetPage);
        });
      }
      return btn;
    }

    buttons.push(makeBtn("←", safePage - 1, safePage <= 1, false));

    const pagesToShow = [];
    if (totalPages <= 7) {
      for (let p = 1; p <= totalPages; p++) {
        pagesToShow.push(p);
      }
    } else {
      pagesToShow.push(1);
      if (safePage > 3) {
        pagesToShow.push("...");
      }
      const winStart = Math.max(2, safePage - 1);
      const winEnd = Math.min(totalPages - 1, safePage + 1);
      for (let p = winStart; p <= winEnd; p++) {
        pagesToShow.push(p);
      }
      if (safePage < totalPages - 2) {
        pagesToShow.push("...");
      }
      pagesToShow.push(totalPages);
    }

    pagesToShow.forEach(function (entry) {
      if (entry === "...") {
        const span = document.createElement("span");
        span.className = "pagination-label";
        span.style.padding = "0 4px";
        span.textContent = "…";
        buttons.push(span);
      } else {
        buttons.push(makeBtn(entry, entry, false, entry === safePage));
      }
    });

    buttons.push(makeBtn("→", safePage + 1, safePage >= totalPages, false));
    btnGroupEl.replaceChildren(...buttons);
  }

  // --- Background Polling for Non-Blocking Jobs ---

  function stopPolling() {
    if (pollTimerId) {
      clearInterval(pollTimerId);
      pollTimerId = null;
    }
  }

  function schedulePolling(fn) {
    stopPolling();
    pollTimerId = setInterval(fn, 2000);
  }

  // --- URL Routing (`/`, `/billing`, `/request/<request-id>`) ---

  function parseRouteRequestId(pathname) {
    const match = /^\/request\/([A-Za-z0-9_-]+)\/?$/.exec(pathname || "");
    return match ? match[1] : null;
  }

  function hideAllScreens() {
    viewDashboard.classList.add("hidden");
    viewWizard.classList.add("hidden");
    viewRequestDetail.classList.add("hidden");
    viewBilling.classList.add("hidden");
    navTabRequests.classList.remove("active");
    navTabBilling.classList.remove("active");
  }

  function navigateTo(path, skipPush) {
    stopPolling();
    if (!skipPush && window.location.pathname !== path) {
      window.history.pushState({}, "", path);
    }
    const reqId = parseRouteRequestId(path);
    if (reqId) {
      openRequestDetailView(reqId);
    } else if (path === "/billing") {
      openBillingView();
    } else {
      openDashboardView();
    }
  }

  function openDashboardView() {
    hideAllScreens();
    navTabRequests.classList.add("active");
    viewDashboard.classList.remove("hidden");
    if (hasLoadedRequestsOnce) {
      renderRequestsTable(cachedRequests);
      fetchRequestsList(true);
    } else {
      setKpisLoading(
        ["kpi-total-requests", "kpi-upscaled-images", "kpi-skipped-images",
          "kpi-total-spend"],
        true
      );
      renderTableLoader(requestsTableTbody, 9, t("loading_requests"), 4);
      fetchRequestsList(false);
    }
  }

  function applyPricingSnapshot(pricingObj) {
    if (!pricingObj || typeof pricingObj !== "object") {
      return;
    }
    const enginesList =
      Array.isArray(pricingObj.engines) ? pricingObj.engines : [];
    enginesList.forEach(function (item) {
      const targetSpec = ENGINE_COST_MAP[item.engine_id];
      if (targetSpec) {
        if (typeof item.cost_500_per_img === "number") {
          targetSpec.cost500 = item.cost_500_per_img;
        }
        if (typeof item.cost_1500_per_img === "number") {
          targetSpec.cost1500 = item.cost_1500_per_img;
        }
      }
    });

    const srcEl = document.getElementById("pricing-sync-source");
    const tsEl = document.getElementById("pricing-sync-timestamp");
    const badgeEl = document.getElementById("pricing-sync-badge");
    if (srcEl && pricingObj.pricing_source) {
      srcEl.textContent = pricingObj.pricing_source;
    }
    if (tsEl && pricingObj.last_synced_utc) {
      tsEl.textContent =
        "Last synced (UTC): " + pricingObj.last_synced_utc.replace("T",
          " ").replace("Z", " UTC");
    }
    if (badgeEl && pricingObj.cloud_billing_api_status) {
      badgeEl.textContent =
        pricingObj.cloud_billing_api_status.indexOf("LIVE") === 0
          ? "Live GCP Catalog Synced"
          : "GCP Official Rate Table Synced";
    }

    const r = pricingObj.unit_rates || {};
    const cpuPill = document.getElementById("rate-pill-cpu");
    const memPill = document.getElementById("rate-pill-mem");
    const gpuPill = document.getElementById("rate-pill-gpu");
    const gcsPill = document.getElementById("rate-pill-gcs");
    const vtxPill = document.getElementById("rate-pill-vertex");
    if (cpuPill && typeof r.cloud_run_vcpu_per_sec_usd === "number") {
      cpuPill.textContent =
        "Cloud Run vCPU: $" + r.cloud_run_vcpu_per_sec_usd.toFixed(6) + "/s";
    }
    if (memPill && typeof r.cloud_run_mem_gib_per_sec_usd === "number") {
      memPill.textContent =
        "Cloud Run RAM: $" + r.cloud_run_mem_gib_per_sec_usd.toFixed(7) +
          "/GiB·s";
    }
    if (gpuPill && typeof r.cloud_run_nvidia_l4_per_hr_usd === "number") {
      gpuPill.textContent =
        "NVIDIA L4 GPU: $" + r.cloud_run_nvidia_l4_per_hr_usd.toFixed(2) +
          "/hr";
    }
    if (gcsPill && typeof r.gcs_class_a_put_per_1k_ops_usd === "number") {
      gcsPill.textContent =
        "GCS Class A PUT: $" + r.gcs_class_a_put_per_1k_ops_usd.toFixed(3) +
          "/1K ops";
    }
    if (vtxPill && typeof r.vertex_batch_discount_factor === "number") {
      const pctOff = Math.round((1 - r.vertex_batch_discount_factor) * 100);
      vtxPill.textContent = "Vertex Batch Discount: " + pctOff + "% OFF";
    }

    renderBillingCalculator();
    updateWizardCostBadges();
  }

  async function fetchPricingSnapshot() {
    try {
      const resp = await fetch("/api/pricing", {
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
      if (resp.ok) {
        const snap = await resp.json();
        applyPricingSnapshot(snap);
      }
    } catch (e) {
      // Non-fatal
    }
  }

  async function syncGcpPricingWithBackend() {
    const syncBtn = document.getElementById("btn-sync-gcp-pricing");
    setButtonLoading(syncBtn, true, t("loading_syncing_pricing"));
    setLoading(true);
    try {
      const resp = await fetch("/api/pricing/sync", {
        method: "POST",
        credentials: "same-origin",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        showToast("Failed to sync GCP pricing (HTTP " + resp.status + ").",
          "error");
        return;
      }
      const snap = await resp.json();
      applyPricingSnapshot(snap);
      showToast(
        "Synced latest unit rates from " +
          (snap.pricing_source || "Google Cloud Billing Catalog") +
          " (" +
          snap.last_synced_utc +
          ").",
        "info"
      );
    } catch (err) {
      showToast("Network error while syncing GCP pricing.", "error");
    } finally {
      setButtonLoading(syncBtn, false);
      setLoading(false);
    }
  }

  function openBillingView() {
    hideAllScreens();
    navTabBilling.classList.add("active");
    viewBilling.classList.remove("hidden");
    renderBillingCalculator();
    fetchPricingSnapshot();
    if (hasLoadedRequestsOnce) {
      updateSummaryKpis(cachedRequests);
      renderBillingHistoryTable(cachedRequests);
      fetchRequestsList(true);
    } else {
      setKpisLoading(["billing-kpi-spend", "billing-kpi-images",
        "billing-kpi-avg"], true);
      renderTableLoader(
        document.getElementById("billing-requests-tbody"),
        8,
        t("loading_billing_history"),
        3
      );
      fetchRequestsList(false);
    }
  }

  function resetWizardState() {
    selectedFilename = "";
    selectedCsvText = "";
    detectedRowCount = 0;
    selectedEngine = "local-lanczos-canvas";
    selectedResolution = 1500;
    if (fileUpload) {
      fileUpload.value = "";
    }
    dropzoneArea.classList.remove("file-ready");
    dropzoneTitle.textContent = t("dropzone_default_title");
    dropzoneSubtitle.textContent = t("dropzone_default_sub");
    btnStep1Next.disabled = true;

    document.querySelectorAll("#engine-cards-container .choice-card")
      .forEach(function (c) {
      c.classList.toggle("selected",
        c.getAttribute("data-engine") === selectedEngine);
    });
    document.querySelectorAll("#resolution-cards-container .choice-card")
      .forEach(function (c) {
      c.classList.toggle("selected",
        c.getAttribute("data-res") === String(selectedResolution));
    });
  }

  function openWizardView() {
    stopPolling();
    hideAllScreens();
    navTabRequests.classList.add("active");
    resetWizardState();
    viewWizard.classList.remove("hidden");
    setWizardStep(1);
  }

  function openRequestDetailView(requestId) {
    hideAllScreens();
    navTabRequests.classList.add("active");
    const isSwitchingJob =
      currentDetailRequestId !== requestId ||
      !detailCurrentRequest ||
      detailCurrentRequest.request_id !== requestId;
    if (isSwitchingJob) {
      detailCurrentRequest = null;
      detailItemsCache = [];
      detailStatusFilter = "ALL";
      detailSearchQuery = "";
      detailCurrentPage = 1;
      const searchInput = document.getElementById("detail-search-input");
      if (searchInput) {
        searchInput.value = "";
      }
      setKpisLoading(
        [
          "detail-kpi-engine",
          "detail-kpi-upscaled",
          "detail-kpi-skipped",
          "detail-kpi-failed",
          "detail-kpi-cost",
        ],
        true
      );
      const progressCard = document.getElementById("detail-live-progress-card");
      if (progressCard) {
        progressCard.classList.add("hidden");
      }
      renderTableLoader(
        document.getElementById("detail-items-tbody"),
        5,
        t("loading_sku_items"),
        5
      );
    }
    currentDetailRequestId = requestId;
    viewRequestDetail.classList.remove("hidden");
    document.getElementById("detail-route-badge").textContent =
      "/request/" + requestId;
    document.getElementById("detail-request-id").textContent = requestId;

    // Reset initial status pill to PROCESSING before network fetch finishes
    const statusChipContainer = document.getElementById("detail-status-chip");
    const initChip = createStatusChip("PROCESSING");
    initChip.id = "detail-status-chip";
    statusChipContainer.replaceWith(initChip);
    document.getElementById("detail-error-alert").classList.add("hidden");
    document.getElementById("detail-failed-skus-card").classList.add("hidden");

    fetchRequestDetail(requestId, false);
  }

  // --- Session & Auth ---

  function setAuthenticatedState(isAuthenticated) {
    if (isAuthenticated) {
      viewAuth.classList.add("hidden");
      authenticatedShell.classList.remove("hidden");
      navTabs.classList.remove("hidden");
      btnLogout.classList.remove("hidden");
      navigateTo(window.location.pathname, true);
    } else {
      stopPolling();
      viewAuth.classList.remove("hidden");
      authenticatedShell.classList.add("hidden");
      navTabs.classList.add("hidden");
      btnLogout.classList.add("hidden");
    }
  }

  async function initializeSession() {
    applyLanguage(getSavedLanguage());
    setLoading(true);
    try {
      const resp = await fetch("/api/session", {
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
      if (!resp.ok) {
        showToast("Unable to connect to server.", "error");
        return;
      }
      const data = await resp.json();
      csrfToken = data.csrf_token || "";
      todayYyyymmdd = data.today_yyyymmdd || "";
      if (data.pricing) {
        applyPricingSnapshot(data.pricing);
      }
      if (Array.isArray(data.requests)) {
        cachedRequests = data.requests;
        hasLoadedRequestsOnce = true;
      }
      setAuthenticatedState(Boolean(data.authenticated));
    } catch (err) {
      showToast("Network error while initializing session.", "error");
    } finally {
      setLoading(false);
    }
  }

  async function handleLogin() {
    const code = inputAccessCode.value.trim();
    if (!code) {
      showToast("Please enter your deployment Access Code.", "error");
      return;
    }
    const btnLogin = document.getElementById("btn-login");
    setButtonLoading(btnLogin, true, t("loading_signing_in"));
    inputAccessCode.disabled = true;
    setLoading(true);
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        credentials: "same-origin",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ access_code: code }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        showToast(data.detail || "Invalid Access Code.", "error");
        return;
      }
      activeAccessCode = code;
      inputAccessCode.value = "";
      showToast("", "info");
      if (data.pricing) {
        applyPricingSnapshot(data.pricing);
      }
      if (Array.isArray(data.requests)) {
        cachedRequests = data.requests;
        hasLoadedRequestsOnce = true;
      }
      setAuthenticatedState(true);
    } catch (err) {
      showToast("Network error while verifying Access Code.", "error");
    } finally {
      inputAccessCode.disabled = false;
      setButtonLoading(btnLogin, false);
      setLoading(false);
    }
  }

  async function handleLogout() {
    stopPolling();
    try {
      await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
    } finally {
      csrfToken = "";
      activeAccessCode = "";
      hasLoadedRequestsOnce = false;
      cachedRequests = [];
      requestsTableTbody.replaceChildren();
      window.location.href = "/";
    }
  }

  // --- Dashboard & Billing Data Rendering ---

  function updateSummaryKpis(records) {
    setKpisLoading(
      [
        "kpi-total-requests",
        "kpi-upscaled-images",
        "kpi-skipped-images",
        "kpi-total-spend",
        "billing-kpi-spend",
        "billing-kpi-images",
        "billing-kpi-avg",
      ],
      false
    );
    const list = records || [];
    let upscaled = 0;
    let skipped = 0;
    let totalSpend = 0;
    list.forEach(function (r) {
      upscaled += Number(r.upscaled_images || 0);
      skipped += Number(r.skipped_images || 0);
      totalSpend += Number(r.estimated_cost_usd || 0);
    });
    document.getElementById("kpi-total-requests").textContent =
      String(list.length);
    document.getElementById("kpi-upscaled-images").textContent =
      String(upscaled);
    document.getElementById("kpi-skipped-images").textContent = String(skipped);
    document.getElementById("kpi-total-spend").textContent =
      formatUsd(totalSpend);

    document.getElementById("billing-kpi-spend").textContent =
      formatUsd(totalSpend);
    document.getElementById("billing-kpi-images").textContent =
      String(upscaled);
    const avgCost = upscaled > 0 ? totalSpend / upscaled : 0;
    document.getElementById("billing-kpi-avg").textContent =
      "$" + avgCost.toFixed(6);
  }

  function renderRequestsTable(records) {
    cachedRequests = records || [];
    hasLoadedRequestsOnce = true;
    updateSummaryKpis(cachedRequests);
    renderBillingHistoryTable(cachedRequests);

    const reqInfoEl = document.getElementById("requests-page-info");
    const reqBtnsEl = document.getElementById("requests-page-buttons");

    if (!cachedRequests.length) {
      const tr = document.createElement("tr");
      const td = createCell("No requests yet.");
      td.setAttribute("colspan", "9");
      tr.appendChild(td);
      requestsTableTbody.replaceChildren(tr);
      renderPaginationControls(reqInfoEl, reqBtnsEl, 0, 1, requestsPageSize,
        function () {});
      return;
    }

    const totalPages =
      Math.max(1, Math.ceil(cachedRequests.length / requestsPageSize));
    if (requestsCurrentPage > totalPages) {
      requestsCurrentPage = totalPages;
    }
    const startIdx = (requestsCurrentPage - 1) * requestsPageSize;
    const pageRecords =
      cachedRequests.slice(startIdx, startIdx + requestsPageSize);

    const rows = pageRecords.map(function (rec) {
      const tr = document.createElement("tr");
      tr.className = "clickable-row";
      tr.addEventListener("click", function (ev) {
        if (ev.target && ev.target.closest("button")) {
          return;
        }
        navigateTo("/request/" + encodeURIComponent(rec.request_id));
      });

      const idTd = document.createElement("td");
      const idLink = document.createElement("a");
      idLink.className = "req-id-link";
      idLink.setAttribute("href", "/request/" +
        encodeURIComponent(rec.request_id));
      idLink.textContent = rec.request_id;
      idLink.addEventListener("click", function (ev) {
        ev.preventDefault();
        navigateTo("/request/" + encodeURIComponent(rec.request_id));
      });
      idTd.appendChild(idLink);
      tr.appendChild(idTd);

      tr.appendChild(createCell(rec.filename));
      tr.appendChild(createCell(ENGINE_LABELS[rec.engine_id] || rec.engine_id));
      tr.appendChild(createCell(rec.target_resolution + " × " +
        rec.target_resolution + " px"));

      const statusTd = document.createElement("td");
      statusTd.appendChild(createStatusChip(rec.status));
      tr.appendChild(statusTd);

      const doneCount =
        Number(rec.upscaled_images || 0) +
        Number(rec.skipped_images || 0) +
        Number(rec.failed_images || 0);
      const totalCount =
        Math.max(1, Number(rec.total_images || rec.total_skus || 0));
      const pct = Math.min(100, Math.round((doneCount / totalCount) * 100));
      const countsText =
        rec.status === "PROCESSING" || rec.status === "QUEUED"
          ? doneCount + " / " + rec.total_images + " (" + pct + "%)"
          : rec.upscaled_images +
            " / " +
            rec.total_images +
            (rec.failed_images > 0 ? " (" + rec.failed_images + " failed)"
              : "");
      tr.appendChild(createCell(countsText));
      tr.appendChild(createCell(formatUsd(rec.estimated_cost_usd)));
      tr.appendChild(createCell(rec.created_at_utc.replace("T",
        " ").replace("Z", "")));

      const actionTd = document.createElement("td");
      actionTd.className = "text-right";
      const btnWrap = document.createElement("div");
      btnWrap.className = "header-btn-group";
      btnWrap.style.justifyContent = "flex-end";

      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "btn btn-google-text";
      openBtn.textContent = t("btn_open_row");
      openBtn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        navigateTo("/request/" + encodeURIComponent(rec.request_id));
      });
      btnWrap.appendChild(openBtn);

      if (rec.status === "COMPLETED" || rec.output_gcs_uri) {
        const dlBtn = document.createElement("button");
        dlBtn.type = "button";
        dlBtn.className = "btn btn-google-outlined";
        dlBtn.textContent = t("btn_dl_csv_short");
        dlBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          downloadWithAuth(
            "/api/requests/" + encodeURIComponent(rec.request_id) +
              "/download-output",
            "out-" + rec.request_id + ".csv",
            dlBtn
          );
        });
        btnWrap.appendChild(dlBtn);
      }

      actionTd.appendChild(btnWrap);
      tr.appendChild(actionTd);
      return tr;
    });

    requestsTableTbody.replaceChildren(...rows);

    renderPaginationControls(
      reqInfoEl,
      reqBtnsEl,
      cachedRequests.length,
      requestsCurrentPage,
      requestsPageSize,
      function (targetPage) {
        requestsCurrentPage = targetPage;
        renderRequestsTable(cachedRequests);
      }
    );

    const hasProcessing = cachedRequests.some(function (r) {
      return r.status === "PROCESSING" || r.status === "QUEUED";
    });
    if (hasProcessing && !viewDashboard.classList.contains("hidden")) {
      schedulePolling(function () {
        fetchRequestsList(true);
      });
    } else if (!hasProcessing && !viewDashboard.classList.contains("hidden")) {
      stopPolling();
    }
  }

  async function fetchRequestsList(silent) {
    const refreshBtn = document.getElementById("btn-refresh-requests");
    if (!silent) {
      setLoading(true);
      setButtonLoading(refreshBtn, true, t("loading_refreshing"));
      if (!hasLoadedRequestsOnce) {
        renderTableLoader(requestsTableTbody, 9, t("loading_requests"), 4);
      }
    }
    try {
      const resp = await fetch("/api/requests", {
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
      if (resp.status === 401) {
        setAuthenticatedState(false);
        return;
      }
      if (!resp.ok) {
        return;
      }
      const records = await resp.json();
      renderRequestsTable(records);
    } catch (err) {
      // Non-fatal refresh error
    } finally {
      if (!silent) {
        setButtonLoading(refreshBtn, false);
        setLoading(false);
      }
    }
  }

  // --- Billing & Cost Estimator Page (`/billing`) ---

  function renderBillingCalculator() {
    const countInput = document.getElementById("calc-image-count");
    const resSelect = document.getElementById("calc-resolution");
    if (!countInput || !resSelect) {
      return;
    }
    const numImages =
      Math.max(1, parseInt(countInput.value || "10000", 10) || 10000);
    const targetRes = parseInt(resSelect.value || "1500", 10) || 1500;

    const tbody = document.getElementById("billing-calculator-tbody");
    const rows = ENGINE_SPECS.map(function (spec, idx) {
      const tr = document.createElement("tr");
      const unitRate = targetRes === 500 ? spec.cost500 : spec.cost1500;
      const ratePer10k = unitRate * 10000;
      const totalEst = unitRate * numImages;

      const engTd = document.createElement("td");
      const strong = document.createElement("strong");
      strong.textContent = spec.name;
      engTd.appendChild(strong);
      if (idx === 0) {
        const badge = document.createElement("span");
        badge.className = "chip chip-green";
        badge.style.marginLeft = "8px";
        badge.textContent = t("badge_recommended");
        engTd.appendChild(badge);
      }
      tr.appendChild(engTd);

      tr.appendChild(createCell("$" + ratePer10k.toFixed(2) + " / 10k"));
      tr.appendChild(createCell(formatUsd(totalEst), "kpi-green"));
      tr.appendChild(createCell(formatDurationEstimate(numImages,
        spec.imgsPerSec)));
      tr.appendChild(createCell(t(spec.bestForKey)));

      const actTd = document.createElement("td");
      actTd.className = "text-right";
      const pickBtn = document.createElement("button");
      pickBtn.type = "button";
      pickBtn.className = "btn btn-google-outlined";
      pickBtn.textContent = t("btn_use_engine");
      pickBtn.addEventListener("click", function () {
        openWizardView();
        selectedEngine = spec.id;
        selectedResolution = targetRes;
        document.querySelectorAll("#engine-cards-container .choice-card")
          .forEach(function (c) {
          c.classList.toggle("selected",
            c.getAttribute("data-engine") === selectedEngine);
        });
        document.querySelectorAll("#resolution-cards-container .choice-card")
          .forEach(function (c) {
          c.classList.toggle("selected",
            c.getAttribute("data-res") === String(selectedResolution));
        });
      });
      actTd.appendChild(pickBtn);
      tr.appendChild(actTd);
      return tr;
    });
    tbody.replaceChildren(...rows);
  }

  function renderBillingHistoryTable(records) {
    const tbody = document.getElementById("billing-requests-tbody");
    if (!records || !records.length) {
      const tr = document.createElement("tr");
      const td = createCell("No requests recorded yet.");
      td.setAttribute("colspan", "8");
      tr.appendChild(td);
      tbody.replaceChildren(tr);
      return;
    }

    const rows = records.slice(0, 50).map(function (rec) {
      const tr = document.createElement("tr");
      tr.className = "clickable-row";
      tr.addEventListener("click", function () {
        navigateTo("/request/" + encodeURIComponent(rec.request_id));
      });

      const idTd = document.createElement("td");
      const link = document.createElement("a");
      link.className = "req-id-link";
      link.setAttribute("href", "/request/" +
        encodeURIComponent(rec.request_id));
      link.textContent = rec.request_id;
      link.addEventListener("click", function (ev) {
        ev.preventDefault();
        navigateTo("/request/" + encodeURIComponent(rec.request_id));
      });
      idTd.appendChild(link);
      tr.appendChild(idTd);

      tr.appendChild(createCell(rec.created_at_utc.replace("T",
        " ").replace("Z", "")));
      tr.appendChild(createCell(rec.filename));
      tr.appendChild(createCell(ENGINE_LABELS[rec.engine_id] || rec.engine_id));
      tr.appendChild(createCell(rec.target_resolution + " × " +
        rec.target_resolution + " px"));
      tr.appendChild(createCell(rec.upscaled_images + " / " +
        rec.total_images));

      const statusTd = document.createElement("td");
      statusTd.appendChild(createStatusChip(rec.status));
      tr.appendChild(statusTd);

      tr.appendChild(createCell(formatUsd(rec.estimated_cost_usd),
        "text-right"));
      return tr;
    });
    tbody.replaceChildren(...rows);
  }

  // --- Request Detail View (`/request/<request-id>`) ---

  function renderDetailItemsPage() {
    const rec = detailCurrentRequest;
    const allItems = detailItemsCache || [];
    const reqStatus = rec ? rec.status : "PROCESSING";

    let countConverted = 0;
    let countSkipped = 0;
    let countFailed = 0;
    let countProcessing = 0;

    const enriched = allItems.map(function (item) {
      const info = extractItemStatusAndError(item, reqStatus);
      if (info.status === "CONVERTED") {
        countConverted++;
      } else if (info.status === "SKIPPED_COMPLIANT") {
        countSkipped++;
      } else if (info.status === "FAILED") {
        countFailed++;
      } else {
        countProcessing++;
      }
      return {
        raw: item,
        normStatus: info.status,
        errorReason: info.errorReason,
        errorType: info.errorType,
      };
    });

    // Update filter pills text and active state
    const pillAll = document.getElementById("filter-pill-all");
    const pillConv = document.getElementById("filter-pill-converted");
    const pillSkip = document.getElementById("filter-pill-skipped");
    const pillFail = document.getElementById("filter-pill-failed");
    const pillProc = document.getElementById("filter-pill-processing");

    if (pillAll) pillAll.textContent = t("filter_all") + " (" +
      enriched.length + ")";
    if (pillConv) pillConv.textContent = t("filter_converted") + " (" +
      countConverted + ")";
    if (pillSkip) pillSkip.textContent = t("filter_skipped") + " (" +
      countSkipped + ")";
    if (pillFail) {
      pillFail.textContent = t("filter_failed") + " (" + countFailed + ")";
      pillFail.classList.toggle("has-errors", countFailed > 0);
    }
    if (pillProc) pillProc.textContent = t("filter_processing") + " (" +
      countProcessing + ")";

    document.querySelectorAll("#detail-status-filters .filter-pill")
      .forEach(function (btn) {
      btn.classList.toggle("active",
        btn.getAttribute("data-filter") === detailStatusFilter);
    });

    const clearFilterBtn = document.getElementById("btn-clear-failed-filter");
    if (clearFilterBtn) {
      clearFilterBtn.classList.toggle("hidden", detailStatusFilter === "ALL" &&
        !detailSearchQuery);
    }

    // Apply status filter + search filter
    const q = String(detailSearchQuery || "").trim().toLowerCase();
    const filtered = enriched.filter(function (entry) {
      if (detailStatusFilter !== "ALL" &&
        entry.normStatus !== detailStatusFilter) {
        return false;
      }
      if (!q) {
        return true;
      }
      const skuMatch =
        String(entry.raw.sku_id || "").toLowerCase().indexOf(q) !== -1;
      const urlMatch =
        String(entry.raw.source_image_url ||
          "").toLowerCase().indexOf(q) !== -1;
      const errMatch =
        String(entry.errorReason || "").toLowerCase().indexOf(q) !== -1;
      return skuMatch || urlMatch || errMatch;
    });

    const tbody = document.getElementById("detail-items-tbody");
    const pageInfoEl = document.getElementById("detail-page-info");
    const pageBtnsEl = document.getElementById("detail-page-buttons");

    if (!filtered.length) {
      const tr = document.createElement("tr");
      let msg = "No matching SKU items found for the selected filter.";
      if (!enriched.length) {
        msg =
          reqStatus === "PROCESSING" || reqStatus === "QUEUED"
            ? "Processing " + (rec ? rec.total_images : 0) +
              " image(s) in parallel... This page updates automatically."
            : "No per-image items recorded for this request.";
      }
      const td = createCell(msg);
      td.setAttribute("colspan", "5");
      tr.appendChild(td);
      tbody.replaceChildren(tr);
      renderPaginationControls(pageInfoEl, pageBtnsEl, 0, 1, detailPageSize,
        function () {});
      return;
    }

    const totalPages = Math.max(1, Math.ceil(filtered.length / detailPageSize));
    if (detailCurrentPage > totalPages) {
      detailCurrentPage = totalPages;
    }
    const startIdx = (detailCurrentPage - 1) * detailPageSize;
    const pageItems = filtered.slice(startIdx, startIdx + detailPageSize);

    const itemRows = pageItems.map(function (entry) {
      const item = entry.raw;
      const tr = document.createElement("tr");
      tr.className = "clickable-row";
      tr.title = "Click to inspect SKU " + item.sku_id + " or retry conversion";
      tr.addEventListener("click", function (ev) {
        if (ev.target && ev.target.closest("a")) {
          return;
        }
        openSkuDetailModal(entry);
      });

      const skuTd = document.createElement("td");
      const skuBtn = document.createElement("span");
      skuBtn.className = "req-id-link";
      skuBtn.textContent = item.sku_id;
      skuTd.appendChild(skuBtn);
      tr.appendChild(skuTd);

      // Status column: Status pill + compact error type pill when FAILED
      const statusTd = document.createElement("td");
      const statusStack = document.createElement("div");
      statusStack.className = "sku-status-cell-stack";
      statusStack.appendChild(createStatusChip(entry.normStatus));
      if (entry.normStatus === "FAILED" && entry.errorType) {
        const errTypePill = document.createElement("span");
        errTypePill.className = "sku-error-type-pill";
        errTypePill.textContent = "⚠️ " + entry.errorType;
        statusStack.appendChild(errTypePill);
      }
      statusTd.appendChild(statusStack);
      tr.appendChild(statusTd);

      const dimText =
        entry.normStatus === "PROCESSING"
          ? "Processing... → " + (rec ? rec.target_resolution + "x" +
            rec.target_resolution : "—")
          : item.original_dimensions + " → " + item.output_dimensions;
      tr.appendChild(createCell(dimText));

      const srcTd = document.createElement("td");
      if (item.source_image_url &&
        item.source_image_url.indexOf("https://") === 0) {
        const srcLink = document.createElement("a");
        srcLink.className = "req-id-link url-cell-truncate";
        srcLink.setAttribute("href", item.source_image_url);
        srcLink.setAttribute("target", "_blank");
        srcLink.setAttribute("rel", "noopener noreferrer");
        srcLink.textContent = item.source_image_url;
        srcTd.appendChild(srcLink);
      } else {
        srcTd.textContent = item.source_image_url || "—";
      }
      tr.appendChild(srcTd);

      const outTd = document.createElement("td");
      if (entry.normStatus === "FAILED") {
        const errBox = document.createElement("div");
        errBox.className = "sku-error-detail-box";
        const errPill = createStatusChip("FAILED");
        errBox.appendChild(errPill);
        const errText = document.createElement("span");
        errText.textContent = entry.errorReason;
        errBox.appendChild(errText);
        outTd.appendChild(errBox);
      } else if (entry.normStatus === "PROCESSING") {
        const procSpan = document.createElement("span");
        procSpan.className = "pagination-label";
        procSpan.textContent = "⏳ " + t("progress_live") + "...";
        outTd.appendChild(procSpan);
      } else if (item.converted_gcs_url &&
        item.converted_gcs_url.indexOf("https://") === 0) {
        const wrap = document.createElement("div");
        wrap.className = "converted-preview-wrap";

        if (item.converted_gcs_url.indexOf(
          "https://storage.googleapis.com/") === 0) {
          const thumb = document.createElement("img");
          thumb.className = "converted-thumb";
          thumb.setAttribute("loading", "lazy");
          thumb.setAttribute("src", item.converted_gcs_url);
          thumb.setAttribute("alt", item.sku_id);
          wrap.appendChild(thumb);
        }

        const outLink = document.createElement("a");
        outLink.className = "req-id-link url-cell-truncate";
        outLink.setAttribute("href", item.converted_gcs_url);
        outLink.setAttribute("target", "_blank");
        outLink.setAttribute("rel", "noopener noreferrer");
        outLink.textContent =
          item.converted_gcs_url.replace("https://storage.googleapis.com/", "");
        wrap.appendChild(outLink);

        if (rec && item.engine_id && item.engine_id !== rec.engine_id) {
          const engBadge = document.createElement("span");
          engBadge.className = "chip chip-blue";
          engBadge.style.fontSize = "11px";
          engBadge.style.padding = "2px 6px";
          engBadge.textContent =
            ENGINE_LABELS[item.engine_id] || item.engine_id;
          wrap.appendChild(engBadge);
        }

        outTd.appendChild(wrap);
      } else {
        outTd.textContent = "—";
      }
      tr.appendChild(outTd);
      return tr;
    });

    tbody.replaceChildren(...itemRows);

    renderPaginationControls(
      pageInfoEl,
      pageBtnsEl,
      filtered.length,
      detailCurrentPage,
      detailPageSize,
      function (targetPage) {
        detailCurrentPage = targetPage;
        renderDetailItemsPage();
      }
    );
  }

  function formatSecondsCompact(secVal) {
    const s = Math.max(0, Math.round(Number(secVal || 0)));
    if (s < 60) {
      return s + "s";
    }
    const mins = Math.floor(s / 60);
    const rem = s % 60;
    return mins + "m " + rem + "s";
  }

  function computeProgressEtaText(rec, completedSkus, totalSkus, isProcessing) {
    const createdMs =
      rec && rec.created_at_utc ? Date.parse(rec.created_at_utc) : NaN;
    const updatedMs =
      rec && rec.updated_at_utc ? Date.parse(rec.updated_at_utc) : NaN;
    const nowMs = Date.now();
    let elapsedSec = 1;
    if (!Number.isNaN(createdMs)) {
      const refEndMs =
        isProcessing ? nowMs : !Number.isNaN(updatedMs) ? updatedMs : nowMs;
      elapsedSec = Math.max(1, Math.round((refEndMs - createdMs) / 1000));
    }

    if (!isProcessing) {
      const durSec =
        rec && Number(rec.duration_ms) > 0
          ? (Number(rec.duration_ms) / 1000).toFixed(1) + "s"
          : formatSecondsCompact(elapsedSec);
      return t("eta_completed_fmt").replace("{elapsed}", durSec);
    }

    const baselineSecPerSkuMap = {
      "local-lanczos-canvas": 0.04,
      realesrgan: 0.18,
      "gemini-3.1-flash-lite-image": 1.6,
      "gemini-3.1-flash-image": 2.1,
      "gemini-3-pro-image": 2.8,
    };
    const engId = (rec && rec.engine_id) || "local-lanczos-canvas";
    const baselineRate = baselineSecPerSkuMap[engId] || 0.25;

    let effectiveSecPerSku = baselineRate;
    if (completedSkus > 0) {
      const observedRate = elapsedSec / completedSkus;
      effectiveSecPerSku =
        completedSkus >= 3
          ? 0.75 * observedRate + 0.25 * baselineRate
          : 0.4 * observedRate + 0.6 * baselineRate;
    }

    const remainingSkus = Math.max(0, totalSkus - completedSkus);
    const etaSec = Math.max(1, Math.ceil(remainingSkus * effectiveSecPerSku));
    return t("eta_remaining_fmt")
      .replace("{eta}", formatSecondsCompact(etaSec))
      .replace("{elapsed}", formatSecondsCompact(elapsedSec));
  }

  function buildPreviewProxyUrl(rawUrl, cacheBustToken) {
    const clean = String(rawUrl || "").trim();
    if (!clean) {
      return "";
    }
    if (
      clean.indexOf("https://") === 0 ||
      clean.indexOf("http://") === 0 ||
      clean.indexOf("synthetic://") === 0
    ) {
      let proxyUrl = "/api/preview-source?url=" + encodeURIComponent(clean);
      if (cacheBustToken) {
        proxyUrl += "&v=" + encodeURIComponent(String(cacheBustToken));
      }
      return proxyUrl;
    }
    return "";
  }

  function setComparatorSplit(pct) {
    const numeric = Math.max(0, Math.min(100, Number(pct)));
    const stageEl = document.getElementById("sku-comp-stage");
    if (stageEl) {
      stageEl.style.setProperty("--split-pct", numeric + "%");
    }
    const overlayRange = document.getElementById("sku-comp-range");
    if (overlayRange && Number(overlayRange.value) !== numeric) {
      overlayRange.value = String(numeric);
    }
    const barRange = document.getElementById("sku-comp-range-bar");
    if (barRange && Number(barRange.value) !== numeric) {
      barRange.value = String(numeric);
    }
  }

  function openSkuDetailModal(entry) {
    if (!entry || !entry.raw) {
      return;
    }
    const item = entry.raw;
    const rec = detailCurrentRequest;
    activeModalSkuId = item.sku_id;

    const modalEl = document.getElementById("sku-detail-modal");
    if (!modalEl) {
      return;
    }

    document.getElementById("sku-modal-sku-id").textContent = item.sku_id;
    const oldChip = document.getElementById("sku-modal-status-chip");
    const nextChip = createStatusChip(entry.normStatus);
    nextChip.id = "sku-modal-status-chip";
    oldChip.replaceWith(nextChip);

    const errBox = document.getElementById("sku-modal-error-box");
    const errTypeEl = document.getElementById("sku-modal-error-type");
    const errTextEl = document.getElementById("sku-modal-error-text");
    if (entry.normStatus === "FAILED") {
      errTypeEl.textContent = "⚠️ " + (entry.errorType || "Error");
      errTextEl.textContent =
        entry.errorReason || "Image download or conversion failed.";
      errBox.classList.remove("hidden");
    } else {
      errBox.classList.add("hidden");
    }

    const engUsed =
      item.engine_id || (rec && rec.engine_id) || "local-lanczos-canvas";
    const targetRes =
      Number(item.target_resolution || (rec && rec.target_resolution) || 1500);
    const spec = ENGINE_COST_MAP[engUsed] || ENGINE_SPECS[0];
    const unitCost = targetRes === 500 ? spec.cost500 : spec.cost1500;
    const skuCost = entry.normStatus === "CONVERTED" ? unitCost : 0.0;

    document.getElementById("sku-modal-req-id").textContent =
      item.request_id || (rec && rec.request_id) || "—";
    document.getElementById("sku-modal-engine").textContent =
      ENGINE_LABELS[engUsed] || engUsed;
    document.getElementById("sku-modal-dims").textContent =
      (item.original_dimensions || "0x0") + " → " + (item.output_dimensions ||
        targetRes + "x" + targetRes);
    document.getElementById("sku-modal-latency").textContent =
      Number(item.processing_ms) > 0 ? Number(item.processing_ms).toFixed(0) +
        " ms" : "—";
    document.getElementById("sku-modal-cost").textContent =
      entry.normStatus === "CONVERTED"
        ? formatUsd(skuCost)
        : entry.normStatus === "SKIPPED_COMPLIANT"
          ? "$0.0000 (Skipped)"
          : "$0.0000 (Not Billed)";

    const srcUrl = (item.source_image_url || "").trim();
    const convUrl = (item.converted_gcs_url || "").trim();
    const effectiveConvUrl =
      convUrl || (entry.normStatus === "SKIPPED_COMPLIANT" ? srcUrl : "");

    // Interactive Before / After Slider Comparator
    const compSection = document.getElementById("sku-comparator-section");
    const compBeforeImg = document.getElementById("sku-comp-before-img");
    const compAfterImg = document.getElementById("sku-comp-after-img");
    const compLoader = document.getElementById("sku-comp-loader");
    const compEmpty = document.getElementById("sku-comp-empty");
    const compBadgeBefore = document.getElementById("sku-comp-badge-before");
    const compBadgeAfter = document.getElementById("sku-comp-badge-after");

    if (compBadgeBefore) {
      compBadgeBefore.textContent = "Before";
    }
    if (compBadgeAfter) {
      compBadgeAfter.textContent = "After";
    }

    if (compSection && compBeforeImg && compAfterImg) {
      compSection.classList.remove("hidden");
      setComparatorSplit(50);

      const beforeProxy = buildPreviewProxyUrl(srcUrl);
      const afterProxy = buildPreviewProxyUrl(
        effectiveConvUrl,
        engUsed + ":" + (item.timestamp_utc || "")
      );

      let pendingCompLoads = (beforeProxy ? 1 : 0) + (afterProxy ? 1 : 0);
      const onCompImgSettled = function () {
        pendingCompLoads -= 1;
        if (pendingCompLoads <= 0 && compLoader) {
          compLoader.classList.add("hidden");
        }
      };

      if (pendingCompLoads > 0 && compLoader) {
        compLoader.classList.remove("hidden");
      } else if (compLoader) {
        compLoader.classList.add("hidden");
      }

      if (beforeProxy) {
        compBeforeImg.classList.remove("hidden");
        let beforeFallbackTried = false;
        compBeforeImg.onload = onCompImgSettled;
        compBeforeImg.onerror = function () {
          if (!beforeFallbackTried && (srcUrl.indexOf("https://") === 0 ||
            srcUrl.indexOf("http://") === 0)) {
            beforeFallbackTried = true;
            compBeforeImg.setAttribute("src", srcUrl);
            return;
          }
          compBeforeImg.classList.add("hidden");
          onCompImgSettled();
        };
        compBeforeImg.setAttribute("src", beforeProxy);
      } else {
        compBeforeImg.removeAttribute("src");
        compBeforeImg.classList.add("hidden");
      }

      if (afterProxy) {
        if (compEmpty) {
          compEmpty.classList.add("hidden");
        }
        compAfterImg.classList.remove("hidden");
        let afterFallbackTried = false;
        compAfterImg.onload = onCompImgSettled;
        compAfterImg.onerror = function () {
          if (
            !afterFallbackTried &&
            (effectiveConvUrl.indexOf("https://") === 0 ||
              effectiveConvUrl.indexOf("http://") === 0)
          ) {
            afterFallbackTried = true;
            compAfterImg.setAttribute("src", effectiveConvUrl);
            return;
          }
          compAfterImg.classList.add("hidden");
          onCompImgSettled();
        };
        compAfterImg.setAttribute("src", afterProxy);
      } else {
        compAfterImg.removeAttribute("src");
        compAfterImg.classList.add("hidden");
        if (compEmpty) {
          compEmpty.textContent =
            entry.normStatus === "FAILED"
              ? "Conversion failed — click Retry below" : "Pending conversion";
          compEmpty.classList.remove("hidden");
        }
      }
    }

    const retrySelect = document.getElementById("sku-modal-retry-engine");
    if (retrySelect) {
      retrySelect.value = engUsed;
    }

    modalEl.classList.remove("hidden");
  }

  function closeSkuDetailModal() {
    const modalEl = document.getElementById("sku-detail-modal");
    if (modalEl) {
      modalEl.classList.add("hidden");
    }
    activeModalSkuId = "";
  }

  async function handleRetrySingleSku() {
    if (!currentDetailRequestId || !activeModalSkuId) {
      return;
    }
    const retryBtn = document.getElementById("btn-sku-modal-retry");
    const compLoader = document.getElementById("sku-comp-loader");
    const retrySelect = document.getElementById("sku-modal-retry-engine");
    const chosenEngine = retrySelect ? retrySelect.value : undefined;
    const targetSku = activeModalSkuId;

    setButtonLoading(retryBtn, true, t("loading_retrying_sku"));
    if (compLoader) {
      compLoader.classList.remove("hidden");
    }
    setLoading(true);
    try {
      const resp = await fetch(
        "/api/requests/" +
          encodeURIComponent(currentDetailRequestId) +
          "/skus/" +
          encodeURIComponent(targetSku) +
          "/retry",
        {
          method: "POST",
          credentials: "same-origin",
          headers: buildAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ engine_id: chosenEngine }),
        }
      );
      const data = await resp.json();
      if (!resp.ok) {
        showToast(data.detail || "Failed to retry SKU " + targetSku + ".",
          "error");
        return;
      }

      renderRequestDetail(data);
      fetchRequestsList(true);

      // Refresh modal with updated SKU item & updated billing
      const updatedItem = (data.items || []).find(function (it) {
        return it.sku_id === targetSku;
      });
      if (updatedItem) {
        const info =
          extractItemStatusAndError(updatedItem, data.request.status);
        openSkuDetailModal({
          raw: updatedItem,
          normStatus: info.status,
          errorReason: info.errorReason,
          errorType: info.errorType,
        });
      }
      showToast(
        "SKU " +
          targetSku +
          " retried — status: " +
          (updatedItem ? updatedItem.status : "UPDATED") +
          " · Job billing updated to " +
          formatUsd(data.request.estimated_cost_usd) +
          ".",
        "info"
      );
    } catch (err) {
      showToast("Network error while retrying SKU " + targetSku + ".", "error");
    } finally {
      setButtonLoading(retryBtn, false);
      setLoading(false);
    }
  }

  async function handleRetryAllFailedSkus() {
    if (!currentDetailRequestId) {
      return;
    }
    const btn = document.getElementById("btn-retry-all-failed");
    setButtonLoading(btn, true, t("loading_retrying_all"));
    setLoading(true);
    try {
      const resp = await fetch(
        "/api/requests/" + encodeURIComponent(currentDetailRequestId) +
          "/retry-failed",
        {
          method: "POST",
          credentials: "same-origin",
          headers: buildAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({}),
        }
      );
      const data = await resp.json();
      if (!resp.ok) {
        showToast(data.detail || "Failed to retry failed SKUs.", "error");
        return;
      }
      renderRequestDetail(data);
      fetchRequestsList(true);
      showToast(
        "Retried failed SKUs — " +
          data.request.upscaled_images +
          " converted · Job billing updated to " +
          formatUsd(data.request.estimated_cost_usd) +
          ".",
        "info"
      );
    } catch (err) {
      showToast("Network error while retrying failed SKUs.", "error");
    } finally {
      setButtonLoading(btn, false);
      setLoading(false);
    }
  }

  function renderRequestDetail(data) {
    const rec = data.request;
    const items = data.items || [];
    detailCurrentRequest = rec;
    detailItemsCache = items;

    setKpisLoading(
      [
        "detail-kpi-engine",
        "detail-kpi-upscaled",
        "detail-kpi-skipped",
        "detail-kpi-failed",
        "detail-kpi-cost",
      ],
      false
    );

    document.getElementById("detail-request-id").textContent = rec.request_id;
    const statusChipContainer = document.getElementById("detail-status-chip");
    const newChip = createStatusChip(rec.status);
    newChip.id = "detail-status-chip";
    statusChipContainer.replaceWith(newChip);

    const isProcessing = rec.status === "PROCESSING" || rec.status === "QUEUED";

    let liveConverted = 0;
    let liveSkipped = 0;
    let liveFailed = 0;
    const failedEntries = [];

    items.forEach(function (it) {
      const info = extractItemStatusAndError(it, rec.status);
      if (info.status === "CONVERTED") {
        liveConverted++;
      } else if (info.status === "SKIPPED_COMPLIANT") {
        liveSkipped++;
      } else if (info.status === "FAILED") {
        liveFailed++;
        failedEntries.push({
          skuId: it.sku_id,
          errorReason: info.errorReason,
          errorType: info.errorType,
          rawItem: it,
        });
      }
    });

    const displayUpscaled =
      Math.max(Number(rec.upscaled_images || 0), liveConverted);
    const displaySkipped =
      Math.max(Number(rec.skipped_images || 0), liveSkipped);
    const displayFailed = Math.max(Number(rec.failed_images || 0), liveFailed);
    const totalSkus =
      Math.max(items.length, Number(rec.total_images || rec.total_skus || 0));
    const completedSkus =
      Math.min(totalSkus, displayUpscaled + displaySkipped + displayFailed);
    const progressPct =
      totalSkus > 0 ? Math.min(100,
        Math.round((completedSkus / totalSkus) * 100)) : 0;

    document.getElementById("detail-kpi-engine").textContent =
      ENGINE_LABELS[rec.engine_id] || rec.engine_id;
    document.getElementById("detail-kpi-upscaled").textContent =
      String(displayUpscaled);
    document.getElementById("detail-kpi-skipped").textContent =
      String(displaySkipped);
    document.getElementById("detail-kpi-failed").textContent =
      String(displayFailed);
    document.getElementById("detail-kpi-cost").textContent =
      formatUsd(rec.estimated_cost_usd);

    // Update Live Parallel Progress Card + Estimated Time Remaining (ETA)
    const progressCard = document.getElementById("detail-live-progress-card");
    const progressFill = document.getElementById("detail-progress-fill");
    const progressStats = document.getElementById("detail-progress-stats");
    const progressEta = document.getElementById("detail-progress-eta");
    const progressTrack = document.getElementById("detail-progress-track");
    const progressPulse = document.getElementById("detail-live-pulse");
    const progressLabel = document.getElementById("detail-progress-label");

    if (progressCard && totalSkus > 0) {
      progressCard.classList.remove("hidden");
      const finalPct = isProcessing ? progressPct : 100;
      const finalDone = isProcessing ? completedSkus : totalSkus;
      if (progressFill) {
        progressFill.style.width = finalPct + "%";
      }
      if (progressTrack) {
        progressTrack.setAttribute("aria-valuenow", String(finalPct));
      }
      if (progressStats) {
        progressStats.textContent =
          finalDone + " / " + totalSkus + " SKUs (" + finalPct + "%)";
      }
      if (progressEta) {
        progressEta.textContent =
          computeProgressEtaText(rec, finalDone, totalSkus, isProcessing);
      }
      if (progressPulse) {
        progressPulse.classList.toggle("done", !isProcessing);
      }
      if (progressLabel) {
        progressLabel.textContent =
          isProcessing ? t("progress_live") : t("progress_completed");
      }
    } else if (progressCard) {
      progressCard.classList.add("hidden");
    }

    const errAlert = document.getElementById("detail-error-alert");
    if (rec.error_message && !isProcessing) {
      errAlert.textContent = "Note: " + rec.error_message;
      errAlert.classList.remove("hidden");
    } else {
      errAlert.classList.add("hidden");
    }

    // Build Failed SKUs clickable pill bar when any SKU failed
    const failedCard = document.getElementById("detail-failed-skus-card");
    const failedListEl = document.getElementById("detail-failed-skus-list");

    if (failedEntries.length > 0 && failedCard && failedListEl) {
      const pills = failedEntries.slice(0, 50).map(function (f) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "sku-error-pill";
        btn.title = "Click to inspect & retry SKU " + f.skuId;

        const idSpan = document.createElement("span");
        idSpan.className = "sku-error-pill-id";
        idSpan.textContent = "⚠️ " + f.skuId;
        btn.appendChild(idSpan);

        const msgSpan = document.createElement("span");
        msgSpan.className = "sku-error-pill-msg";
        msgSpan.textContent = "— " + f.errorReason;
        btn.appendChild(msgSpan);

        btn.addEventListener("click", function () {
          detailStatusFilter = "FAILED";
          detailSearchQuery = f.skuId;
          detailCurrentPage = 1;
          const searchInput = document.getElementById("detail-search-input");
          if (searchInput) {
            searchInput.value = f.skuId;
          }
          renderDetailItemsPage();
          if (f.rawItem) {
            openSkuDetailModal({
              raw: f.rawItem,
              normStatus: "FAILED",
              errorReason: f.errorReason,
              errorType: f.errorType,
            });
          }
        });
        return btn;
      });
      failedListEl.replaceChildren(...pills);
      failedCard.classList.remove("hidden");
    } else if (failedCard) {
      failedCard.classList.add("hidden");
    }

    renderDetailItemsPage();

    if (isProcessing) {
      schedulePolling(function () {
        if (currentDetailRequestId === rec.request_id) {
          fetchRequestDetail(rec.request_id, true);
        }
      });
    } else {
      stopPolling();
    }
  }

  async function fetchRequestDetail(requestId, silent) {
    const refreshBtn = document.getElementById("btn-detail-refresh");
    if (!silent) {
      setLoading(true);
      setButtonLoading(refreshBtn, true, t("loading_refreshing"));
    }
    try {
      const resp =
        await fetch("/api/requests/" + encodeURIComponent(requestId), {
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
      if (resp.status === 401) {
        setAuthenticatedState(false);
        return;
      }
      if (!resp.ok) {
        showToast("Request '" + requestId + "' was not found.", "error");
        return;
      }
      const data = await resp.json();
      renderRequestDetail(data);
    } catch (err) {
      if (!silent) {
        showToast("Network error while loading request details.", "error");
      }
    } finally {
      if (!silent) {
        setLoading(false);
        setButtonLoading(refreshBtn, false);
      }
    }
  }

  // --- Multi-Screen Animated Request Wizard ---

  function countCsvRows(text) {
    const lines = String(text || "")
      .split(/\r?\n/)
      .map(function (l) {
        return l.trim();
      })
      .filter(Boolean);
    if (!lines.length) {
      return 0;
    }
    const urlPattern = /(?:https?:\/\/|synthetic:\/\/)/gi;
    const firstMatches = lines[0].match(urlPattern);
    const dataLines = firstMatches && firstMatches.length > 0
      ? lines
      : lines.slice(1);
    if (!dataLines.length) {
      return 0;
    }
    let totalImages = 0;
    dataLines.forEach(function (line) {
      const matches = line.match(urlPattern);
      if (matches && matches.length > 0) {
        totalImages += Math.min(11, matches.length);
      } else {
        totalImages += 1;
      }
    });
    return totalImages;
  }

  function setCsvPayload(filename, text) {
    selectedFilename = filename;
    selectedCsvText = text;
    detectedRowCount = countCsvRows(text);
    if (detectedRowCount > 0) {
      dropzoneArea.classList.add("file-ready");
      dropzoneTitle.textContent = filename + " (" + detectedRowCount + " rows)";
      dropzoneSubtitle.textContent = "Ready";
      btnStep1Next.disabled = false;
    } else {
      dropzoneArea.classList.remove("file-ready");
      dropzoneTitle.textContent = "No valid image rows found in " + filename;
      dropzoneSubtitle.textContent = t("dropzone_default_sub");
      btnStep1Next.disabled = true;
    }
  }

  function updateWizardCostBadges() {
    const count = Math.max(1, detectedRowCount || 1);
    ENGINE_SPECS.forEach(function (spec, idx) {
      const badgeEl = document.getElementById("wiz-cost-" + spec.id);
      if (!badgeEl) {
        return;
      }
      const est = estimateJobCost(spec.id, selectedResolution, count);
      badgeEl.textContent =
        (idx === 0 ? t("badge_recommended") + " · " : "") + formatUsd(est);
    });
  }

  function setWizardStep(stepNumber) {
    document.getElementById("wizard-step-label").textContent =
      "Step " + stepNumber + " / 3";

    [1, 2, 3].forEach(function (n) {
      const panel = document.getElementById("wizard-step-" + n);
      const node = document.getElementById("stepper-node-" + n);
      if (n === stepNumber) {
        panel.classList.remove("hidden");
        node.className = "step-item active";
      } else if (n < stepNumber) {
        panel.classList.add("hidden");
        node.className = "step-item completed";
      } else {
        panel.classList.add("hidden");
        node.className = "step-item";
      }
    });

    document.getElementById("stepper-line-1").className =
      stepNumber > 1 ? "step-connector completed" : "step-connector";
    document.getElementById("stepper-line-2").className =
      stepNumber > 2 ? "step-connector completed" : "step-connector";

    if (stepNumber === 2) {
      updateWizardCostBadges();
    } else if (stepNumber === 3) {
      document.getElementById("review-filename").textContent =
        selectedFilename || "uploaded.csv";
      document.getElementById("review-row-count").textContent =
        String(detectedRowCount);
      document.getElementById("review-engine").textContent =
        ENGINE_LABELS[selectedEngine] || selectedEngine;
      document.getElementById("review-resolution").textContent =
        selectedResolution + " × " + selectedResolution + " px";
      const estCost =
        estimateJobCost(selectedEngine, selectedResolution, detectedRowCount);
      document.getElementById("review-cost").textContent = formatUsd(estCost);
    }
  }

  async function handleSubmitRequest() {
    if (!selectedCsvText) {
      showToast("Please select a CSV file first.", "error");
      setWizardStep(1);
      return;
    }

    const payloadFilename = selectedFilename || "uploaded.csv";
    const payloadCsv = selectedCsvText;
    const payloadEngine = selectedEngine;
    const payloadRes = selectedResolution;

    setButtonLoading(btnSubmitRequest, true, t("loading_submitting"));
    setLoading(true);

    try {
      const resp = await fetch("/api/requests", {
        method: "POST",
        credentials: "same-origin",
        headers: buildAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          filename: payloadFilename,
          csv_content: payloadCsv,
          engine_id: payloadEngine,
          target_resolution: payloadRes,
          async_job: true,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        showToast(data.detail || "Failed to submit request.", "error");
        return;
      }

      resetWizardState();
      navigateTo("/request/" + encodeURIComponent(data.request_id));
      showToast(
        "Job " + data.request_id +
          " queued — processing in parallel in the background.",
        "info"
      );
    } catch (err) {
      showToast("Network error while submitting request.", "error");
    } finally {
      setButtonLoading(btnSubmitRequest, false);
      setLoading(false);
    }
  }

  // --- Authenticated File Downloads ---

  async function downloadWithAuth(urlPath, filename, triggerBtn) {
    setLoading(true);
    if (triggerBtn) {
      setButtonLoading(triggerBtn, true, t("loading_downloading"));
    }
    try {
      const resp = await fetch(urlPath, {
        credentials: "same-origin",
        headers: buildAuthHeaders(),
      });
      if (!resp.ok) {
        showToast("Failed to download " + filename + " (HTTP " + resp.status +
          ").", "error");
        return;
      }
      const csvText = await resp.text();
      const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
      const objUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.setAttribute("href", objUrl);
      link.setAttribute("download", filename);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(objUrl);
    } catch (err) {
      showToast("Network error while downloading " + filename + ".", "error");
    } finally {
      setLoading(false);
      if (triggerBtn) {
        setButtonLoading(triggerBtn, false);
      }
    }
  }

  function getSampleCsvString() {
    return (
      "id,image_link,additional_image_link\n" +
      "SKU-1001,https://encrypted-tbn0.gstatic.com/shopping?q=tbn:ANd9GcQJ3Xa" +
      "K98ATkUigYEZg49J4iOkGWhI-YXYZxYkH_PUzE1xt5J69z_onYRtyVKBn3-pUXHKQY2iZM" +
      "3pv8jNlyxLG1KCqSbR0gQ,\n" +
      "SKU-1002,https://encrypted-tbn2.gstatic.com/shopping?q=tbn:ANd9GcTft0z" +
      "h_QBdJfc79nx8KjQW-UqAM_Ca7ZDFilTcfP29g9sCCoZ5FCQyqg9kA9-YRG5-CQY036m8e" +
      "M15Fl-C1HciFjyvl2olwGFW4yA-Tu0S0JUmQ9Nw9WQW,\n" +
      "SKU-1003,https://encrypted-tbn2.gstatic.com/shopping?q=tbn:ANd9GcSffEg" +
      "E0tbJ_4PajXTKAIMfqAeC7ceqkey0tMRj_SX5D9QnwEefeHDOcrsINEpAV_rQ5ym9ZlunL" +
      "u750_hh6A_uXma_8-mJ03Uu_SNNJj4rh1eYf4EwObDt,\n"
    );
  }

  // --- Event Listeners ---

  selectLanguage.addEventListener("change", function () {
    applyLanguage(selectLanguage.value);
  });

  document.getElementById("nav-brand-link")
    .addEventListener("click", function (ev) {
    ev.preventDefault();
    if (!authenticatedShell.classList.contains("hidden")) {
      navigateTo("/");
    }
  });
  navTabRequests.addEventListener("click", function (ev) {
    ev.preventDefault();
    navigateTo("/");
  });
  navTabBilling.addEventListener("click", function (ev) {
    ev.preventDefault();
    navigateTo("/billing");
  });

  document.getElementById("btn-login").addEventListener("click", handleLogin);
  inputAccessCode.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      handleLogin();
    }
  });
  btnLogout.addEventListener("click", handleLogout);

  // Open Wizard buttons
  document.getElementById("btn-hero-new-request")
    .addEventListener("click", openWizardView);
  document.getElementById("btn-billing-new-request")
    .addEventListener("click", openWizardView);
  document.getElementById("btn-wizard-cancel")
    .addEventListener("click", function () {
    navigateTo("/");
  });

  const btnSyncPricing = document.getElementById("btn-sync-gcp-pricing");
  if (btnSyncPricing) {
    btnSyncPricing.addEventListener("click", syncGcpPricingWithBackend);
  }

  // Wizard Step navigation
  fileUpload.addEventListener("change", function (ev) {
    const file = ev.target.files && ev.target.files[0];
    if (!file) {
      return;
    }
    dropzoneTitle.textContent = t("loading_parsing_csv");
    dropzoneSubtitle.textContent = file.name;
    btnStep1Next.disabled = true;
    const reader = new FileReader();
    reader.onload = function (loadEv) {
      setCsvPayload(file.name, String(loadEv.target.result || ""));
    };
    reader.readAsText(file);
  });

  document.getElementById("btn-download-sample")
    .addEventListener("click", function () {
    const blob =
      new Blob([getSampleCsvString()], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", "sample_images.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  });

  btnStep1Next.addEventListener("click", function () {
    setWizardStep(2);
  });
  document.getElementById("btn-step2-back")
    .addEventListener("click", function () {
    setWizardStep(1);
  });
  document.getElementById("btn-step2-next")
    .addEventListener("click", function () {
    setWizardStep(3);
  });
  document.getElementById("btn-step3-back")
    .addEventListener("click", function () {
    setWizardStep(2);
  });
  btnSubmitRequest.addEventListener("click", handleSubmitRequest);

  // Interactive Engine & Resolution choice cards in Step 2
  document.querySelectorAll("#engine-cards-container .choice-card")
    .forEach(function (card) {
    card.addEventListener("click", function () {
      document.querySelectorAll("#engine-cards-container .choice-card")
        .forEach(function (c) {
        c.classList.remove("selected");
      });
      card.classList.add("selected");
      selectedEngine =
        card.getAttribute("data-engine") || "local-lanczos-canvas";
    });
  });

  document.querySelectorAll("#resolution-cards-container .choice-card")
    .forEach(function (card) {
    card.addEventListener("click", function () {
      document.querySelectorAll("#resolution-cards-container .choice-card")
        .forEach(function (c) {
        c.classList.remove("selected");
      });
      card.classList.add("selected");
      selectedResolution =
        parseInt(card.getAttribute("data-res") || "1500", 10);
      updateWizardCostBadges();
    });
  });

  // Billing Calculator live inputs
  document.getElementById("calc-image-count")
    .addEventListener("input", renderBillingCalculator);
  document.getElementById("calc-resolution")
    .addEventListener("change", renderBillingCalculator);

  // Dashboard & Detail actions
  document.getElementById("btn-refresh-requests")
    .addEventListener("click", function () {
    fetchRequestsList(false);
  });
  const btnDailyLog = document.getElementById("btn-download-daily-log");
  btnDailyLog.addEventListener("click", function () {
    const dateKey = todayYyyymmdd || "20261005";
    downloadWithAuth(
      "/api/status-log/" + encodeURIComponent(dateKey),
      "log_" + dateKey + ".csv",
      btnDailyLog
    );
  });

  document.getElementById("btn-detail-back")
    .addEventListener("click", function () {
    navigateTo("/");
  });
  document.getElementById("btn-detail-refresh")
    .addEventListener("click", function () {
    if (currentDetailRequestId) {
      fetchRequestDetail(currentDetailRequestId, false);
    }
  });
  const btnDetailOut = document.getElementById("btn-detail-download-output");
  btnDetailOut.addEventListener("click", function () {
    if (currentDetailRequestId) {
      downloadWithAuth(
        "/api/requests/" + encodeURIComponent(currentDetailRequestId) +
          "/download-output",
        "out-" + currentDetailRequestId + ".csv",
        btnDetailOut
      );
    }
  });
  const btnDetailIn = document.getElementById("btn-detail-download-input");
  btnDetailIn.addEventListener("click", function () {
    if (currentDetailRequestId) {
      downloadWithAuth(
        "/api/requests/" + encodeURIComponent(currentDetailRequestId) +
          "/download-input",
        "in-" + currentDetailRequestId + ".csv",
        btnDetailIn
      );
    }
  });

  // Individual SKU Modal & Retry listeners
  const btnCloseModal = document.getElementById("btn-close-sku-modal");
  if (btnCloseModal) {
    btnCloseModal.addEventListener("click", closeSkuDetailModal);
  }
  const btnCloseModalBottom =
    document.getElementById("btn-sku-modal-close-bottom");
  if (btnCloseModalBottom) {
    btnCloseModalBottom.addEventListener("click", closeSkuDetailModal);
  }
  const skuModalBackdrop = document.getElementById("sku-detail-modal");
  if (skuModalBackdrop) {
    skuModalBackdrop.addEventListener("click", function (ev) {
      if (ev.target === skuModalBackdrop) {
        closeSkuDetailModal();
      }
    });
  }
  const btnModalRetry = document.getElementById("btn-sku-modal-retry");
  if (btnModalRetry) {
    btnModalRetry.addEventListener("click", handleRetrySingleSku);
  }
  const btnRetryAllFailed = document.getElementById("btn-retry-all-failed");
  if (btnRetryAllFailed) {
    btnRetryAllFailed.addEventListener("click", handleRetryAllFailedSkus);
  }

  // Interactive Before/After Image Comparator slider listeners
  const compRangeOverlay = document.getElementById("sku-comp-range");
  if (compRangeOverlay) {
    compRangeOverlay.addEventListener("input", function () {
      setComparatorSplit(compRangeOverlay.value);
    });
  }
  const compRangeBar = document.getElementById("sku-comp-range-bar");
  if (compRangeBar) {
    compRangeBar.addEventListener("input", function () {
      setComparatorSplit(compRangeBar.value);
    });
  }
  const btnComp0 = document.getElementById("btn-comp-0");
  if (btnComp0) {
    btnComp0.addEventListener("click", function () {
      setComparatorSplit(0);
    });
  }
  const btnComp50 = document.getElementById("btn-comp-50");
  if (btnComp50) {
    btnComp50.addEventListener("click", function () {
      setComparatorSplit(50);
    });
  }
  const btnComp100 = document.getElementById("btn-comp-100");
  if (btnComp100) {
    btnComp100.addEventListener("click", function () {
      setComparatorSplit(100);
    });
  }

  // Pagination & Filter Controls
  const reqPageSizeSelect = document.getElementById("requests-page-size");
  if (reqPageSizeSelect) {
    reqPageSizeSelect.addEventListener("change", function () {
      requestsPageSize =
        Math.max(5, parseInt(reqPageSizeSelect.value || "20", 10) || 20);
      requestsCurrentPage = 1;
      renderRequestsTable(cachedRequests);
    });
  }

  const detailPageSizeSelect = document.getElementById("detail-page-size");
  if (detailPageSizeSelect) {
    detailPageSizeSelect.addEventListener("change", function () {
      detailPageSize =
        Math.max(10, parseInt(detailPageSizeSelect.value || "25", 10) || 25);
      detailCurrentPage = 1;
      renderDetailItemsPage();
    });
  }

  document.querySelectorAll("#detail-status-filters .filter-pill")
    .forEach(function (pill) {
    pill.addEventListener("click", function () {
      detailStatusFilter = pill.getAttribute("data-filter") || "ALL";
      detailCurrentPage = 1;
      renderDetailItemsPage();
    });
  });

  const detailSearchInput = document.getElementById("detail-search-input");
  if (detailSearchInput) {
    detailSearchInput.addEventListener("input", function () {
      detailSearchQuery = detailSearchInput.value || "";
      detailCurrentPage = 1;
      renderDetailItemsPage();
    });
  }

  const failedKpiCard = document.getElementById("detail-kpi-failed-card");
  if (failedKpiCard) {
    failedKpiCard.addEventListener("click", function () {
      detailStatusFilter = detailStatusFilter === "FAILED" ? "ALL" : "FAILED";
      detailCurrentPage = 1;
      renderDetailItemsPage();
    });
  }

  const clearFailedBtn = document.getElementById("btn-clear-failed-filter");
  if (clearFailedBtn) {
    clearFailedBtn.addEventListener("click", function () {
      detailStatusFilter = "ALL";
      detailSearchQuery = "";
      detailCurrentPage = 1;
      if (detailSearchInput) {
        detailSearchInput.value = "";
      }
      renderDetailItemsPage();
    });
  }

  window.addEventListener("popstate", function () {
    navigateTo(window.location.pathname, true);
  });

  if (typeof globalThis !== "undefined") {
    globalThis.__IMAGE_UPSCALER_TEST_HOOKS__ = {
      I18N: I18N,
      ENGINE_SPECS: ENGINE_SPECS,
      t: t,
      applyLanguage: applyLanguage,
      formatUsd: formatUsd,
      formatDurationEstimate: formatDurationEstimate,
      estimateJobCost: estimateJobCost,
      extractItemStatusAndError: extractItemStatusAndError,
      parseRouteRequestId: parseRouteRequestId,
      formatSecondsCompact: formatSecondsCompact,
      computeProgressEtaText: computeProgressEtaText,
      buildPreviewProxyUrl: buildPreviewProxyUrl,
      setComparatorSplit: setComparatorSplit,
      countCsvRows: countCsvRows,
      getSampleCsvString: getSampleCsvString,
      applyPricingSnapshot: applyPricingSnapshot,
    };
  }

  initializeSession();
})();
