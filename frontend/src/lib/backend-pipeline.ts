import {
  AnalystBundle,
  AnalystExceptionRow,
  AnalystExplainQuestion,
  AnalystExplainResponse,
  AnalystSourceCitation,
  DatabookMetricRecord,
  Deal,
  DefinedItem,
  ExceptionItem,
  FormulaInputAssignment,
  FinalMetricRecord,
  MappingRow,
  MappingStatus,
  OutputAsset,
  OutputStatus,
  RecentActivityItem,
  ReviewIssueClass,
  RowRoutingBucket,
  Severity,
  SourceFile,
  TraceabilityRecord,
} from "@/lib/types";
import { isBlockingCoreIssue } from "@/lib/review-utils";
import { formatDateTime } from "@/lib/utils";

const BACKEND_PROXY_BASE = "/api/process-run";
const PILOT_RUNS_API = "/api/pilot-runs";
const unmappedCategory = "Unmapped";

type BackendExtractionBackend = "deterministic" | "gemini";
type BackendValidationStatus = "pass" | "warning" | "fail";

export interface BackendPilotRunSummary {
  run_id: string;
  created_at: string;
  status: "completed" | "failed";
  output_dir: string;
  workbook_path: string;
  extraction_backend: BackendExtractionBackend;
  artifact_paths: Record<string, string>;
  input_paths: Record<string, string | null>;
  notes: string[];
  run_label?: string | null;
  validation_status?: BackendValidationStatus | null;
  issue_count?: number | null;
  document_count?: number | null;
}

interface BackendSourceDocument {
  source_id: string;
  file_name: string;
  rel_path: string;
  file_type: string;
  parser_used: string;
  priority_rank: number;
}

interface BackendSourceManifest {
  data_room_dir: string;
  indexed_at: string;
  document_count: number;
  skipped_count: number;
  documents: BackendSourceDocument[];
}

interface BackendEvidenceRef {
  evidence_id: string;
  source_id: string;
  locator_label: string;
  quote: string;
  file_name: string;
  page_number?: number | null;
  sheet_name?: string | null;
  cell_range?: string | null;
  section_name?: string | null;
  extraction_method: "heuristic" | "parser" | "llm";
  confidence: number;
}

interface BackendMetricValue {
  value: number;
  raw_value: string;
  unit_scale: "ones" | "thousands" | "millions" | "percent" | "count";
  currency?: string | null;
  evidence_refs: BackendEvidenceRef[];
  confidence: number;
}

interface BackendPnlExtractionRecord {
  extraction_id: string;
  source_id: string;
  source_file_name: string;
  period_label?: string | null;
  period_key?: string | null;
  period_granularity: "month" | "quarter" | "year" | "ltm" | "unknown";
  revenue?: BackendMetricValue | null;
  direct_costs?: BackendMetricValue | null;
  gross_profit?: BackendMetricValue | null;
  operating_expenses?: BackendMetricValue | null;
  ebitda?: BackendMetricValue | null;
  adjusted_ebitda?: BackendMetricValue | null;
  customer_concentration_pct?: BackendMetricValue | null;
  employee_count?: BackendMetricValue | null;
  notes: string[];
  uncertainty: string[];
  conflicting_values: Record<string, string[]>;
}

interface BackendExtractionBundle {
  schema_name: "pnl_v1";
  record_count: number;
  records: BackendPnlExtractionRecord[];
  assumptions: string[];
}

interface BackendResolvedMetricValue {
  value: number;
  unit_scale: "ones" | "thousands" | "millions" | "percent" | "count";
  currency?: string | null;
  source_ids: string[];
  evidence_refs: BackendEvidenceRef[];
  status: "provided" | "derived";
  formula?: string | null;
  notes: string[];
  conflicting_values: number[];
}

interface BackendResolvedPnlPeriod {
  period_label: string;
  period_key: string;
  revenue?: BackendResolvedMetricValue | null;
  direct_costs?: BackendResolvedMetricValue | null;
  gross_profit?: BackendResolvedMetricValue | null;
  operating_expenses?: BackendResolvedMetricValue | null;
  ebitda?: BackendResolvedMetricValue | null;
  adjusted_ebitda?: BackendResolvedMetricValue | null;
  customer_concentration_pct?: BackendResolvedMetricValue | null;
  employee_count?: BackendResolvedMetricValue | null;
  notes: string[];
}

interface BackendWorkbookBinding {
  sheet_name: string;
  cell: string;
  cell_role: "label" | "header" | "input" | "formula" | "note";
  line_item_code?: string | null;
  period_key?: string | null;
  value?: string | number | null;
  formula?: string | null;
  number_format: string;
  comment?: string | null;
  source_ids: string[];
}

interface BackendSourceMapEntry {
  sheet_name: string;
  cell: string;
  line_item_code: string;
  period_key: string;
  value_display: string;
  source_ids: string[];
  locators: string[];
  quotes: string[];
}

interface BackendValidationIssue {
  severity: "error" | "warning" | "info";
  code: string;
  message: string;
  context: Record<string, unknown>;
}

interface BackendValidationReport {
  status: BackendValidationStatus;
  issue_count: number;
  assumptions: string[];
  issues: BackendValidationIssue[];
}

interface BackendAnalystSourceCitation {
  file: string;
  tab?: string | null;
  range?: string | null;
  value?: number | null;
  source_id?: string | null;
}

interface BackendFinalMetricRecord {
  metric_key: string;
  metric_name: string;
  period: string;
  period_key: string;
  period_order: number;
  final_value?: number | null;
  unit?: string | null;
  selected_source?: BackendAnalystSourceCitation | null;
  backup_sources: BackendAnalystSourceCitation[];
  source_priority_reason?: string | null;
  direct_or_derived: "direct" | "derived";
  derivation_formula?: string | null;
  validation_result: "Matched" | "Formula" | "Single-source" | "Mismatch";
  confidence_level: "High" | "Medium" | "Low";
  confidence_reason: string;
  status: "Ready" | "Review";
  note?: string | null;
  cross_check_log: string[];
}

interface BackendAnalystExceptionRow {
  metric: string;
  period: string;
  issue: string;
  system_view: string;
  suggested_action: string;
  severity: "Info" | "Review" | "Critical";
  related_metric_key?: string | null;
  related_period_key?: string | null;
}

interface BackendAnalystBundle {
  metrics: BackendFinalMetricRecord[];
  exceptions: BackendAnalystExceptionRow[];
  period_order: string[];
  period_keys: string[];
  metric_order: string[];
}

export interface BackendPilotRunPayload {
  summary: BackendPilotRunSummary;
  source_manifest: BackendSourceManifest;
  extraction_bundle: BackendExtractionBundle;
  resolved_periods: BackendResolvedPnlPeriod[];
  workbook_bindings: BackendWorkbookBinding[];
  source_map_entries: BackendSourceMapEntry[];
  validation_report: BackendValidationReport;
  analyst_bundle?: BackendAnalystBundle | null;
}

interface CreateBackendDealInput {
  dealName: string;
  sector: string;
  uploads: Array<{
    file?: File;
    name: string;
    fileType: string;
    detectedCategory: Deal["sourceFiles"][number]["detectedCategory"];
    uploadDate?: string;
  }>;
}

const metricFieldConfigs = [
  {
    field: "revenue",
    lineItemCode: "revenue",
    key: "revenue",
    label: "Revenue",
    mappedCategory: "Revenue",
    outputLineKey: "revenue",
    format: "currency" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "direct_costs",
    lineItemCode: "direct_costs",
    key: "cogs",
    label: "COGS",
    mappedCategory: "COGS",
    outputLineKey: "cogs",
    format: "currency" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "gross_profit",
    lineItemCode: "gross_profit",
    key: "gross-profit",
    label: "Gross Profit",
    mappedCategory: "Gross Profit",
    outputLineKey: "gross_profit",
    format: "currency" as const,
    directOrDerived: "Derived" as const,
    calculationType: "Formula" as const,
    dependencies: ["Revenue", "COGS"],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "gross_margin_pct",
    lineItemCode: "gross_margin_pct",
    key: "gross-margin",
    label: "Gross Margin",
    mappedCategory: "Gross Profit",
    outputLineKey: "gross_margin",
    format: "percentage" as const,
    directOrDerived: "Derived" as const,
    calculationType: "Ratio" as const,
    dependencies: ["Gross Profit", "Revenue"],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "operating_expenses",
    lineItemCode: "operating_expenses",
    key: "operating-expenses",
    label: "Operating Expenses",
    mappedCategory: "Operating Expenses",
    outputLineKey: "operating_expenses",
    format: "currency" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "ebitda",
    lineItemCode: "ebitda",
    key: "ebitda",
    label: "EBITDA",
    mappedCategory: "EBITDA",
    outputLineKey: "ebitda",
    format: "currency" as const,
    directOrDerived: "Derived" as const,
    calculationType: "Formula" as const,
    dependencies: ["Gross Profit", "Operating Expenses"],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "ebitda_margin_pct",
    lineItemCode: "ebitda_margin_pct",
    key: "ebitda-margin",
    label: "EBITDA Margin",
    mappedCategory: "EBITDA",
    outputLineKey: "ebitda_margin",
    format: "percentage" as const,
    directOrDerived: "Derived" as const,
    calculationType: "Ratio" as const,
    dependencies: ["EBITDA", "Revenue"],
    routingBucket: "Core Financial" as const,
    entersCorePipeline: true,
  },
  {
    field: "employee_count",
    lineItemCode: "employee_count",
    key: "headcount",
    label: "Headcount",
    mappedCategory: "Headcount",
    outputLineKey: "headcount",
    format: "number" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "KPI Input" as const,
    entersCorePipeline: true,
  },
  {
    field: "customer_concentration_pct",
    lineItemCode: "customer_concentration_pct",
    key: "customer-concentration",
    label: "Customer Concentration",
    mappedCategory: unmappedCategory,
    outputLineKey: "customer_concentration_pct",
    format: "percentage" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "Supporting Detail" as const,
    entersCorePipeline: false,
  },
  {
    field: "adjusted_ebitda",
    lineItemCode: "adjusted_ebitda",
    key: "adjusted-ebitda",
    label: "Adjusted EBITDA",
    mappedCategory: unmappedCategory,
    outputLineKey: "adjusted_ebitda",
    format: "currency" as const,
    directOrDerived: "Direct" as const,
    calculationType: "Source Reported" as const,
    dependencies: [] as string[],
    routingBucket: "Supporting Detail" as const,
    entersCorePipeline: false,
  },
] as const;

export async function fetchPilotRunSummaries(limit = 200): Promise<BackendPilotRunSummary[]> {
  const response = await fetch(`${PILOT_RUNS_API}?limit=${encodeURIComponent(String(limit))}`);
  if (!response.ok) {
    let message = "Unable to list pilot runs.";
    try {
      const payload = (await response.json()) as { error?: string; detail?: string };
      message = payload.error ?? payload.detail ?? message;
    } catch {
      // ignore
    }
    throw new Error(message);
  }
  return (await response.json()) as BackendPilotRunSummary[];
}

export async function fetchPilotRunPayload(runId: string): Promise<BackendPilotRunPayload> {
  const response = await fetch(
    `${PILOT_RUNS_API}/${encodeURIComponent(runId)}/payload`,
  );
  if (!response.ok) {
    let message = "Unable to load this run.";
    try {
      const payload = (await response.json()) as { error?: string; detail?: string };
      message = payload.error ?? payload.detail ?? message;
    } catch {
      // ignore
    }
    throw new Error(message);
  }
  return (await response.json()) as BackendPilotRunPayload;
}

export async function createDealFromBackendRun(
  input: CreateBackendDealInput,
): Promise<Deal> {
  const formData = new FormData();
  formData.append("import_label", input.dealName);
  formData.append("extraction_backend", "gemini");

  for (const upload of input.uploads) {
    if (upload.file) {
      formData.append("files", upload.file, upload.file.name);
    }
  }

  const response = await fetch(BACKEND_PROXY_BASE, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let message = "Backend processing failed.";
    const responseText = await response.text();
    try {
      const payload = JSON.parse(responseText) as { error?: string; detail?: string };
      message = payload.error ?? payload.detail ?? message;
    } catch {
      message = responseText.trim() || message;
    }
    throw new Error(message);
  }

  const payload = (await response.json()) as BackendPilotRunPayload;
  return buildDealFromBackendPayload(input, payload);
}

export function refreshBackendDealState(deal: Deal): Deal {
  const openItems = deal.exceptions.filter((item) => item.status === "Open");
  const blockingItems = openItems.filter((item) => item.issueClass === "real_core_blocker");
  const metrics = deal.databookMetrics ?? [];
  const availableMetrics = metrics.filter((metric) => metric.status !== "Unavailable").length;
  const hasCoreCoverage = ["Revenue", "COGS", "Operating Expenses"].every((label) =>
    metrics.some((metric) => metric.label.startsWith(label) && metric.status !== "Unavailable"),
  );
  const outputsReady = blockingItems.length === 0 && availableMetrics > 0 && hasCoreCoverage;
  const outputs = deal.outputs.map((output) => ({
    ...output,
    status: (outputsReady ? "Ready" : "Needs Review") as OutputStatus,
    reviewStatus: outputsReady
      ? "Backend workbook is ready to export."
      : `${blockingItems.length} blocking issue${blockingItems.length === 1 ? "" : "s"} remain in validation.`,
    rowCount: output.rowCount ?? availableMetrics,
  }));

  return {
    ...deal,
    outputs,
    outputStatus: outputs[0]?.status ?? deal.outputStatus,
    outputsReady,
    exceptionCount: blockingItems.length,
    sourceFilesConnected: deal.sourceFiles.length > 0,
    sourceFileIds: deal.sourceFiles.map((file) => file.id),
    extractionProgress: deal.extractedItems.length > 0 ? 100 : deal.extractionProgress,
    readinessScore: outputsReady ? 94 : blockingItems.length > 0 ? 58 : 76,
    workflowProgress: outputsReady ? 100 : deal.workflowProgress || 78,
  };
}

export function getBackendWorkbookDownloadPath(deal: Deal) {
  return deal.backendRun?.workbookDownloadPath;
}

export function isBackendDealReadyForExport(deal: Deal) {
  if (deal.processingEngine !== "backend_python") {
    return false;
  }

  const blockingItems = deal.exceptions.filter(
    (item) => item.status === "Open" && isBlockingCoreIssue(item),
  ).length;
  const outputMarkedReady =
    deal.outputsReady ||
    deal.outputStatus === "Ready" ||
    deal.outputs.some((output) => output.status === "Ready");

  return outputMarkedReady && blockingItems === 0;
}

export async function requestBackendMetricExplanation(params: {
  runId: string;
  metricKey: string;
  periodKey: string;
  question: AnalystExplainQuestion;
}): Promise<AnalystExplainResponse> {
  const response = await fetch(`/api/process-run/${encodeURIComponent(params.runId)}/explain`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      metric_key: params.metricKey,
      period_key: params.periodKey,
      question: params.question,
    }),
  });

  if (!response.ok) {
    let message = "Unable to explain this number.";
    try {
      const payload = (await response.json()) as { error?: string; detail?: string };
      message = payload.error ?? payload.detail ?? message;
    } catch {
      // Keep fallback.
    }
    throw new Error(message);
  }

  const payload = (await response.json()) as {
    answer: string;
    metric_key: string;
    period_key: string;
    question: AnalystExplainQuestion;
    confidence_level?: AnalystExplainResponse["confidenceLevel"];
    status?: AnalystExplainResponse["status"];
    selected_source_file?: string | null;
    selected_source_tab?: string | null;
    selected_source_range?: string | null;
  };

  return {
    answer: payload.answer,
    metricKey: payload.metric_key,
    periodKey: payload.period_key,
    question: payload.question,
    confidenceLevel: payload.confidence_level ?? null,
    status: payload.status ?? null,
    selectedSourceFile: payload.selected_source_file ?? null,
    selectedSourceTab: payload.selected_source_tab ?? null,
    selectedSourceRange: payload.selected_source_range ?? null,
  };
}

function buildDealFromBackendPayload(
  input: CreateBackendDealInput,
  payload: BackendPilotRunPayload,
): Deal {
  return buildDealFromRunPayload(payload, {
    targetCompanyName: input.dealName || "Uploaded Dataroom",
    sector: input.sector,
  });
}

export function buildDealFromHistoricalPayload(payload: BackendPilotRunPayload): Deal {
  const rawLabel =
    payload.summary.run_label?.trim() || deriveDisplayLabelFromSummary(payload.summary);
  const targetName = rawLabel || `Run ${payload.summary.run_id}`;
  return buildDealFromRunPayload(payload, {
    targetCompanyName: targetName,
    sector: "Historical run",
  });
}

function deriveDisplayLabelFromSummary(summary: BackendPilotRunSummary): string {
  const dir = summary.input_paths?.data_room_dir;
  if (!dir) {
    return "";
  }
  const normalized = dir.replace(/\\/g, "/");
  const segments = normalized.split("/").filter(Boolean);
  const last = segments[segments.length - 1] ?? "";
  if (!last) {
    return "";
  }
  return humanizeUploadFolderName(last);
}

function humanizeUploadFolderName(folder: string): string {
  const withoutSuffix = folder.replace(/-[a-f0-9]{8}$/i, "");
  const spaced = withoutSuffix.replace(/-/g, " ").trim();
  if (!spaced) {
    return folder;
  }
  return spaced.replace(/\b\w/g, (character) => character.toUpperCase());
}

function buildDealFromRunPayload(
  payload: BackendPilotRunPayload,
  meta: { targetCompanyName: string; sector: string },
): Deal {
  const sourceFiles = buildSourceFiles(payload);
  const extractedItems = buildExtractedItems(payload);
  const mappingRows = buildMappingRows(payload);
  const definedItems = buildDefinedItemsFromBackend(payload, sourceFiles, mappingRows);
  const formulaInputs = buildFormulaInputsFromBackend(definedItems);
  const analystBundle = buildAnalystBundle(payload);
  const databookMetrics = buildBackendDatabookMetrics(payload);
  const traceabilityRecords = buildTraceabilityRecordsFromBackend(payload, databookMetrics);
  const exceptions = buildExceptionsFromBackend(payload, analystBundle);
  const outputs = buildOutputAssetsFromBackend(payload, databookMetrics, exceptions, analystBundle);
  const recentActivity = buildRecentActivity(payload, sourceFiles);
  const blockingItems = exceptions.filter(
    (item) => item.status === "Open" && item.issueClass === "real_core_blocker",
  ).length;
  const slugPart = slugify(meta.targetCompanyName) || "import";

  return refreshBackendDealState({
    id: `${slugPart}-${payload.summary.run_id}`,
    targetCompanyName: meta.targetCompanyName,
    sector: meta.sector,
    status: "Extraction",
    outputStatus: outputs[0]?.status ?? "Queued",
    sourceFilesConnected: sourceFiles.length > 0,
    sourceFileIds: sourceFiles.map((file) => file.id),
    extractionProgress: extractedItems.length > 0 ? 100 : 0,
    exceptionCount: blockingItems,
    outputsReady: false,
    seller: "Local upload",
    sponsor: "Undisclosed",
    geography: "Not specified",
    stage: "Import to workbook",
    enterpriseValue: 0,
    ttmRevenue:
      databookMetrics.find((metric) => metric.label.startsWith("Revenue"))?.value ?? 0,
    ttmEbitda:
      databookMetrics.find((metric) => metric.label.startsWith("EBITDA"))?.value ?? 0,
    readinessScore: payload.validation_report.status === "pass" ? 94 : payload.validation_report.status === "warning" ? 78 : 52,
    workflowProgress: 78,
    recentActivity,
    sourceFiles,
    extractedItems,
    mappingRows,
    definedItems,
    formulaInputs,
    databookMetrics,
    traceabilityRecords,
    exceptions,
    outputs,
    analystBundle,
    copilotPrompts: [],
    processingEngine: "backend_python",
    backendRun: {
      runId: payload.summary.run_id,
      extractionBackend: payload.summary.extraction_backend,
      workbookDownloadPath: buildArtifactPath(payload.summary.run_id, "generated_workbook"),
      validationReportPath: buildArtifactPath(payload.summary.run_id, "validation_markdown"),
      resolvedPnlPath: buildArtifactPath(payload.summary.run_id, "resolved_pnl"),
      extractedPnlPath: buildArtifactPath(payload.summary.run_id, "extracted_pnl"),
      sourceMapPath: buildArtifactPath(payload.summary.run_id, "source_map_entries"),
      validationStatus: payload.validation_report.status,
      issueCount: payload.validation_report.issue_count,
    },
    qualityPanel: {
      missingHeaders: payload.validation_report.issues.filter((issue) => issue.code === "missing_required_field").length,
      duplicateFiles: 0,
      unreadablePages: payload.validation_report.issues.filter((issue) => issue.code === "no_pnl_records").length,
      unitAmbiguity: payload.validation_report.issues.filter((issue) => issue.code === "obvious_unit_issue").length,
      confidenceSummary: {
        high: databookMetrics.filter((metric) => metric.status !== "Unavailable" && metric.traceabilityStatus === "Traced").length,
        medium: databookMetrics.filter((metric) => metric.status !== "Unavailable" && metric.traceabilityStatus === "Partial").length,
        low: databookMetrics.filter((metric) => metric.status === "Unavailable").length,
      },
    },
  });
}

function buildSourceFiles(payload: BackendPilotRunPayload): SourceFile[] {
  const segmentCounts = countSegmentsBySource(payload);

  return payload.source_manifest.documents.map((document) => ({
    id: document.source_id,
    name: document.file_name,
    fileType: document.file_type.toUpperCase(),
    uploadDate: payload.summary.created_at,
    detectedCategory: inferCategory(document.file_name),
    status: "Indexed",
    pages: segmentCounts.get(document.source_id) ?? 1,
    owner: "Python pipeline",
    supportedForParsing: true,
  }));
}

function buildExtractedItems(payload: BackendPilotRunPayload) {
  const sourceFiles = new Map(payload.source_manifest.documents.map((document) => [document.source_id, document]));

  return payload.extraction_bundle.records.map((record, index) => {
    const metricRows = getRecordMetrics(record);
    const title = `${record.period_label ?? record.period_key ?? "Undated"} · ${sourceFiles.get(record.source_id)?.file_name ?? record.source_file_name}`;
    const issueFlags = metricRows.length === 0 ? ["No structured rows detected"] : record.uncertainty.length > 0 ? record.uncertainty : ["None"];
    const routingBucket: RowRoutingBucket = metricRows.some((entry) => entry.config.entersCorePipeline)
      ? metricRows.some((entry) => entry.config.routingBucket === "Core Financial")
        ? "Core Financial"
        : "KPI Input"
      : "Supporting Detail";

    return {
      id: `ext-${record.extraction_id}-${index + 1}`,
      title,
      tableName: title,
      sourceFileId: record.source_id,
      period: record.period_label ?? record.period_key ?? "Undated",
      confidence: averageConfidence(metricRows),
      detectedTableType: detectBackendTableType(record),
      issueFlags,
      summary:
        metricRows.length > 0
          ? `${metricRows.length} extracted metrics: ${metricRows.map((metric) => metric.label).join(", ")}.`
          : "No structured metric rows were extracted from this source.",
      rows: metricRows.map((metric) => ({
        label: metric.label,
        value: metric.metric.raw_value,
        location: metric.metric.evidence_refs[0]?.locator_label ?? record.source_file_name,
        rawCells: [metric.label, metric.metric.raw_value],
      })),
      routingBucket,
      coreRowCount: metricRows.filter((entry) => entry.config.entersCorePipeline).length,
      supportingRowCount: metricRows.filter((entry) => !entry.config.entersCorePipeline).length,
    };
  });
}

function buildMappingRows(payload: BackendPilotRunPayload): MappingRow[] {
  return payload.extraction_bundle.records.flatMap((record) =>
    getRecordMetrics(record).map(({ config, metric }) => {
      const sourceLocator =
        metric.evidence_refs[0]?.locator_label ??
        `${record.source_file_name} ${record.period_label ?? record.period_key ?? "Undated"}`;

      return {
        id: `map-${record.extraction_id}-${config.lineItemCode}`,
        sourceFileId: record.source_id,
        sourceLocator,
        rawLineItemLabel: buildRawLabel(config.label, metric),
        rawValue: metric.raw_value,
        period: record.period_label ?? record.period_key ?? "Undated",
        mappedCategory: config.mappedCategory,
        confidence: Math.round(metric.confidence * 100),
        sourceLinked: metric.evidence_refs.length > 0,
        status: resolveMappingStatus(metric),
        reasoning: buildMappingReasoning(config.label, metric, record),
        interpretationProvider:
          payload.summary.extraction_backend === "gemini" &&
          metric.evidence_refs.some((evidence) => evidence.extraction_method === "llm")
            ? "gemini"
            : "deterministic",
        routingBucket: config.routingBucket,
        entersCorePipeline: config.entersCorePipeline,
        routingReason: config.entersCorePipeline
          ? `${config.label} was extracted into the backend workbook pipeline.`
          : `${config.label} is kept as supporting detail outside the core workbook path.`,
      } satisfies MappingRow;
    }),
  );
}

function buildDefinedItemsFromBackend(
  payload: BackendPilotRunPayload,
  sourceFiles: SourceFile[],
  mappingRows: MappingRow[],
): DefinedItem[] {
  const fileIndex = new Map(sourceFiles.map((file) => [file.id, file]));

  return mappingRows.map((row) => {
    const parsed = parseBackendLocator(row.sourceLocator);
    const normalizedValue = parseBackendNumericValue(row.rawValue);
    const unit = detectBackendUnit(row.rawValue, row.mappedCategory);
    const directOrDerived = ["Gross Profit", "EBITDA"].includes(row.mappedCategory) ? "Derived" : "Direct";
    const outputLineKey = mapCategoryToOutputLineKey(row.mappedCategory);

    return {
      id: `defined-${row.id}`,
      sourceFileId: row.sourceFileId,
      sourceFileName: fileIndex.get(row.sourceFileId)?.name ?? "Unknown file",
      sourceSheetName: parsed.sourceSheetName,
      sourceLocation: parsed.sourceLocation,
      period: row.period,
      rawLabel: row.rawLineItemLabel,
      rawValue: row.rawValue,
      normalizedValue,
      unit,
      detectedType: row.mappedCategory === unmappedCategory ? "Supporting Detail" : row.mappedCategory,
      mappedCategory: row.mappedCategory,
      mappedMetric: row.mappedCategory,
      outputLineKey,
      formulaRole: directOrDerived === "Derived" ? "Derived Candidate" : "Input",
      dependencyCandidates: getDependencyCandidates(row.mappedCategory),
      formulaTemplateKey: mapCategoryToFormulaTemplateKey(row.mappedCategory),
      definition: `${row.mappedCategory === unmappedCategory ? "Supporting row" : row.mappedCategory} extracted by the backend pipeline.`,
      rationale: row.reasoning,
      calculationType: directOrDerived === "Derived" ? "Formula" : "Source Reported",
      directOrDerived,
      formulaDependencies: getDependencyCandidates(row.mappedCategory),
      reviewStatus: row.status,
      traceabilityStatus: row.sourceLinked ? "Traced" : "Missing",
      routingBucket: row.routingBucket,
      entersCorePipeline: row.entersCorePipeline,
      routingReason: row.routingReason,
    };
  });
}

function buildFormulaInputsFromBackend(definedItems: DefinedItem[]): FormulaInputAssignment[] {
  return definedItems
    .filter((item) => item.entersCorePipeline !== false && item.mappedCategory !== unmappedCategory)
    .map((item) => ({
      id: `input-${item.id}`,
      definedItemId: item.id,
      sourceFileId: item.sourceFileId,
      sourceFileName: item.sourceFileName,
      sourceSheetName: item.sourceSheetName,
      sourceLocation: item.sourceLocation,
      rawLabel: item.rawLabel,
      rawValue: item.rawValue,
      period: item.period,
      mappedMetric: item.mappedMetric,
      outputLineKey: item.outputLineKey,
      formulaRole: item.formulaRole,
      dependencyCandidates: item.dependencyCandidates,
      formulaTemplateKey: item.formulaTemplateKey,
      directOrDerived: item.directOrDerived,
      normalizedValue: item.normalizedValue,
      assignedValue: item.normalizedValue,
      unit: item.unit,
      rationale: item.rationale,
      reviewStatus: item.reviewStatus,
      traceabilityStatus: item.traceabilityStatus,
    }));
}

function buildBackendDatabookMetrics(payload: BackendPilotRunPayload): DatabookMetricRecord[] {
  const multiplePeriods = payload.resolved_periods.length > 1;
  const bindingIndex = new Map(
    payload.workbook_bindings
      .filter((binding) => binding.period_key && binding.line_item_code)
      .map((binding) => [`${binding.period_key}:${binding.line_item_code}`, binding]),
  );
  const sourceMapIndex = new Map(
    payload.source_map_entries.map((entry) => [`${entry.period_key}:${entry.line_item_code}`, entry]),
  );

  return payload.resolved_periods.flatMap((period) =>
    metricFieldConfigs
      .map((config) => buildMetricFromBackend({
        config,
        period,
        multiplePeriods,
        binding: bindingIndex.get(`${period.period_key}:${config.lineItemCode}`),
        sourceMapEntry: sourceMapIndex.get(`${period.period_key}:${config.lineItemCode}`),
      }))
      .filter((item): item is DatabookMetricRecord => Boolean(item)),
  );
}

function buildTraceabilityRecordsFromBackend(
  payload: BackendPilotRunPayload,
  metrics: DatabookMetricRecord[],
): TraceabilityRecord[] {
  const sourceNameIndex = new Map(
    payload.source_manifest.documents.map((document) => [document.source_id, document.file_name]),
  );
  const sourceMapIndex = new Map<string, (typeof payload.source_map_entries)[number]>();
  for (const entry of payload.source_map_entries) {
    sourceMapIndex.set(`${entry.period_key}:${entry.line_item_code}`, entry);
    // Margin metrics look up by outputLineKey ("gross_margin") while the source
    // map is keyed by line_item_code ("gross_margin_pct") — register the
    // suffix-stripped alias so margin rows resolve their traceability records.
    if (entry.line_item_code.endsWith("_pct")) {
      sourceMapIndex.set(`${entry.period_key}:${entry.line_item_code.replace(/_pct$/, "")}`, entry);
    }
  }

  return metrics.flatMap((metric) => {
    const entry = sourceMapIndex.get(`${metric.period}:${metric.outputLineKey}`);
    if (!entry) {
      return [];
    }

    return entry.locators.map((locator, index) => ({
      id: `trace-${metric.key}-${index + 1}`,
      outputMetricKey: metric.key,
      outputLineKey: metric.outputLineKey,
      outputMetricLabel: metric.label,
      outputMetricValue: metric.formattedValue,
      // locators can outnumber sourceInputIds — don't leak undefined.
      sourceInputId: metric.sourceInputIds[index] ?? metric.sourceInputIds[0] ?? "",
      sourceFileId: entry.source_ids[index],
      sourceFileName:
        sourceNameIndex.get(entry.source_ids[index] ?? "") ?? "Unknown source",
      sourceSheetName: entry.sheet_name,
      sourceLocation: locator,
      rawLabel: entry.quotes[index] ?? metric.label,
      rawValue: entry.value_display,
      period: metric.period,
      mappedCategory: metric.label,
      directOrDerived: metric.directOrDerived,
      formulaDependencies: metric.formulaDependencies,
      derivationPath: metric.formula,
      traceabilityStatus: metric.traceabilityStatus,
    }));
  });
}

function buildAnalystBundle(payload: BackendPilotRunPayload): AnalystBundle | undefined {
  const backendBundle = payload.analyst_bundle;
  if (!backendBundle) {
    return undefined;
  }

  return {
    metrics: backendBundle.metrics.map(buildFinalMetricRecord),
    exceptions: backendBundle.exceptions.map(buildAnalystExceptionRow),
    periodOrder: [...backendBundle.period_order],
    periodKeys: [...backendBundle.period_keys],
    metricOrder: [...backendBundle.metric_order],
  };
}

function buildFinalMetricRecord(record: BackendFinalMetricRecord): FinalMetricRecord {
  return {
    metricKey: record.metric_key,
    metricName: record.metric_name,
    period: record.period,
    periodKey: record.period_key,
    periodOrder: record.period_order,
    finalValue: record.final_value ?? null,
    unit: (record.unit as FinalMetricRecord["unit"]) ?? null,
    selectedSource: record.selected_source ? buildAnalystSourceCitation(record.selected_source) : null,
    backupSources: record.backup_sources.map(buildAnalystSourceCitation),
    sourcePriorityReason: record.source_priority_reason ?? null,
    directOrDerived: record.direct_or_derived,
    derivationFormula: record.derivation_formula ?? null,
    validationResult: record.validation_result,
    confidenceLevel: record.confidence_level,
    confidenceReason: record.confidence_reason,
    status: record.status,
    note: record.note ?? null,
    crossCheckLog: [...record.cross_check_log],
  };
}

function buildAnalystSourceCitation(citation: BackendAnalystSourceCitation): AnalystSourceCitation {
  return {
    file: citation.file,
    tab: citation.tab ?? null,
    range: citation.range ?? null,
    value: citation.value ?? null,
    sourceId: citation.source_id ?? null,
  };
}

function buildAnalystExceptionRow(row: BackendAnalystExceptionRow): AnalystExceptionRow {
  return {
    metric: row.metric,
    period: row.period,
    issue: row.issue,
    systemView: row.system_view,
    suggestedAction: row.suggested_action,
    severity: row.severity,
    relatedMetricKey: row.related_metric_key ?? null,
    relatedPeriodKey: row.related_period_key ?? null,
  };
}

function buildExceptionsFromBackend(
  payload: BackendPilotRunPayload,
  analystBundle?: AnalystBundle,
): ExceptionItem[] {
  const canonicalExceptions = (analystBundle?.exceptions ?? []).map((item, index) =>
    buildExceptionFromAnalystRow(item, index),
  );
  const tableWarnings = payload.validation_report.issues
    .map((issue, index) => ({ issue, index }))
    .filter(({ issue }) => classifyValidationIssue(issue).issueClass === "table_warning")
    .map(({ issue, index }) => buildExceptionFromValidationIssue(issue, index, canonicalExceptions.length));

  if (canonicalExceptions.length > 0) {
    return [...canonicalExceptions, ...tableWarnings];
  }

  return payload.validation_report.issues.map((issue, index) =>
    buildExceptionFromValidationIssue(issue, index, 0),
  );
}

function buildExceptionFromAnalystRow(
  item: AnalystExceptionRow,
  index: number,
): ExceptionItem {
  const isCoreMetricBlocker =
    item.severity === "Critical" ||
    ((item.relatedMetricKey === "revenue" ||
      item.relatedMetricKey === "cogs" ||
      item.relatedMetricKey === "gross_profit" ||
      item.relatedMetricKey === "operating_expenses" ||
      item.relatedMetricKey === "ebitda") &&
      item.issue.toLowerCase().includes("no source"));

  return {
    id: `canonical-review-${index + 1}`,
    issueKey: `${item.relatedMetricKey ?? slugify(item.metric)}-${item.relatedPeriodKey ?? slugify(item.period)}-${index + 1}`,
    origin: "generated",
    scope: "core",
    blocksExport: isCoreMetricBlocker,
    issueLevel: "metric",
    issueClass: isCoreMetricBlocker ? "real_core_blocker" : "non_blocking_mapping_bug",
    severity:
      item.severity === "Critical"
        ? "Critical"
        : item.severity === "Review"
          ? "Medium"
          : "Low",
    category: item.issue,
    affectedLineItem: `${item.metric}${item.period ? ` · ${item.period}` : ""}`,
    suggestedResolution: item.suggestedAction,
    assignedOwner: "Deal team",
    status: "Open",
    detail: item.systemView,
  };
}

function buildExceptionFromValidationIssue(
  issue: BackendValidationIssue,
  index: number,
  baseOffset: number,
): ExceptionItem {
  const classification = classifyValidationIssue(issue);
  const contextField = typeof issue.context.field === "string" ? issue.context.field : undefined;
  const periodKey = typeof issue.context.period_key === "string" ? issue.context.period_key : undefined;

  return {
    id: `backend-review-${baseOffset + index + 1}`,
    issueKey: `${issue.code}-${index + 1}`,
    origin: "generated",
    scope: "core",
    blocksExport: classification.blocksExport,
    issueLevel: classification.issueLevel,
    issueClass: classification.issueClass,
    sourceFileId: typeof issue.context.source_id === "string" ? issue.context.source_id : undefined,
    severity: classification.severity,
    category: classification.category,
    affectedLineItem: contextField
      ? `${humanizeMetricField(contextField)}${periodKey ? ` · ${periodKey}` : ""}`
      : periodKey
        ? `${issue.code.replaceAll("_", " ")} · ${periodKey}`
        : issue.code.replaceAll("_", " "),
    suggestedResolution: classification.suggestedResolution,
    assignedOwner: "Deal team",
    status: "Open",
    detail: issue.message,
  };
}

function buildOutputAssetsFromBackend(
  payload: BackendPilotRunPayload,
  metrics: DatabookMetricRecord[],
  exceptions: ExceptionItem[],
  analystBundle?: AnalystBundle,
): OutputAsset[] {
  const blockingItems = exceptions.filter(
    (item) => item.status === "Open" && item.issueClass === "real_core_blocker",
  );
  const ready = blockingItems.length === 0 && metrics.some((metric) => metric.status !== "Unavailable");
  const status: OutputStatus = ready ? "Ready" : "Needs Review";
  const generatedDate = payload.summary.created_at;
  const previewRows =
    analystBundle && analystBundle.metrics.length > 0
      ? analystBundle.metrics.slice(0, 12).map((metric) => ({
          item: `${metric.metricName}${metric.period ? ` (${metric.period})` : ""}`,
          valueA: formatCanonicalMetricValue(metric),
          valueB: `${metric.confidenceLevel} · ${metric.validationResult}`,
          trace:
            metric.directOrDerived === "derived"
              ? metric.derivationFormula ?? "Derived formula"
              : metric.selectedSource
                ? [metric.selectedSource.file, metric.selectedSource.tab, metric.selectedSource.range]
                    .filter(Boolean)
                    .join(" · ")
                : metric.sourcePriorityReason ?? "Source-selected value",
        }))
      : metrics
          .filter((metric) => metric.status !== "Unavailable")
          .slice(0, 12)
          .map((metric) => ({
            item: metric.label,
            valueA: metric.formattedValue,
            valueB: metric.formula,
            trace: metric.sourceSummary,
          }));

  return [
    {
      id: "databook-preview",
      name: "Databook Preview",
      status,
      generatedDate,
      completeness: ready ? 100 : 72,
      sourceLinked: metrics.every((metric) => metric.traceabilityStatus !== "Missing"),
      reviewStatus: ready
        ? "Backend workbook is ready to export."
        : `${blockingItems.length} blocking validation issue${blockingItems.length === 1 ? "" : "s"} remain.`,
      rowCount: analystBundle?.metrics.length ?? metrics.filter((metric) => metric.status !== "Unavailable").length,
      previewType: "table",
      previewRows,
    },
    {
      id: "pnl-workbook",
      name: "P&L Workbook",
      status,
      generatedDate,
      completeness: ready ? 100 : 72,
      sourceLinked: metrics.every((metric) => metric.traceabilityStatus !== "Missing"),
      reviewStatus: ready
        ? "Workbook formulas and source map were generated by the Python pipeline."
        : "Workbook is still provisional until validation blockers clear.",
      rowCount: analystBundle?.metrics.length ?? metrics.filter((metric) => metric.status !== "Unavailable").length,
      previewType: "sections",
      previewSections: [
        {
          heading: "Workbook output",
          bullets: [
            "Generated by the Python pipeline, not by browser-local heuristics.",
            "Formula cells stay deterministic and source-aware.",
            `Validation status: ${payload.validation_report.status.toUpperCase()}.`,
          ],
        },
      ],
    },
  ];
}

function buildRecentActivity(
  payload: BackendPilotRunPayload,
  sourceFiles: SourceFile[],
): RecentActivityItem[] {
  return [
    {
      id: `activity-${payload.summary.run_id}`,
      title: "Backend pipeline completed",
      description: `${sourceFiles.length} files were parsed through the Python workbook pipeline using ${payload.summary.extraction_backend}.`,
      timestamp: payload.summary.created_at,
    },
    {
      id: `activity-workbook-${payload.summary.run_id}`,
      title: "Workbook written",
      description: "A deterministic Excel workbook with formulas, source map, and validation tabs is ready.",
      timestamp: payload.summary.created_at,
    },
  ];
}

function buildMetricFromBackend(params: {
  config: (typeof metricFieldConfigs)[number];
  period: BackendResolvedPnlPeriod;
  multiplePeriods: boolean;
  binding?: BackendWorkbookBinding;
  sourceMapEntry?: BackendSourceMapEntry;
}): DatabookMetricRecord | null {
  const label = params.multiplePeriods
    ? `${params.config.label} (${params.period.period_label})`
    : params.config.label;
  const key = `${params.config.key}-${slugify(params.period.period_key)}`;
  const directMetric = getPeriodMetric(params.period, params.config.field);
  const derivedValue = deriveMetricValue(params.period, params.config.field);
  const sourceMetric = directMetric ?? derivedValue.metric;

  if (!sourceMetric) {
    return {
      key,
      outputLineKey: params.config.outputLineKey,
      label,
      period: params.period.period_key,
      value: null,
      formattedValue: "Unavailable",
      status: "Unavailable",
      formula: params.binding?.formula ?? fallbackFormulaLabel(params.config.lineItemCode),
      definition: `${params.config.label} was not available in the backend run.`,
      rationale: derivedValue.rationale ?? `Missing inputs for ${params.config.label}.`,
      calculationType: params.config.calculationType,
      directOrDerived: params.config.directOrDerived,
      formulaDependencies: [...params.config.dependencies],
      sourceInputIds: [],
      sourceSummary: params.sourceMapEntry?.locators.join(" | ") ?? `Missing inputs for ${params.config.label}`,
      traceabilityStatus: "Missing",
      format: params.config.format,
    };
  }

  const value = derivedValue.value ?? sourceMetric.value;
  const directOrDerived =
    params.config.directOrDerived === "Derived" || sourceMetric.status === "derived"
      ? "Derived"
      : "Direct";
  const status =
    directOrDerived === "Derived"
      ? "Calculated"
      : "Provided";
  const sourceSummary =
    params.sourceMapEntry?.locators.join(" | ") ||
    sourceMetric.evidence_refs.map((evidence) => evidence.locator_label).join(" | ") ||
    "Source link not captured";

  return {
    key,
    outputLineKey: params.config.outputLineKey,
    label,
    period: params.period.period_key,
    value,
    formattedValue: formatBackendMetricValue(value, params.config.format),
    status,
    formula: params.binding?.formula ?? fallbackFormulaLabel(params.config.lineItemCode),
    definition:
      directOrDerived === "Derived"
        ? `${params.config.label} is formula-backed in the backend workbook.`
        : `${params.config.label} is source-backed in the backend workbook.`,
    rationale:
      derivedValue.rationale ??
      (sourceMetric.status === "derived"
        ? `${params.config.label} was derived deterministically in the Python pipeline.`
        : `${params.config.label} came from backend-resolved source evidence.`),
    calculationType:
      directOrDerived === "Derived"
        ? params.config.calculationType
        : sourceMetric.status === "derived"
          ? "Formula"
          : sourceMetric.source_ids.length > 1
            ? "Aggregation"
            : "Source Reported",
    directOrDerived,
    formulaDependencies: [...params.config.dependencies],
    sourceInputIds: sourceMetric.source_ids,
    sourceSummary,
    traceabilityStatus: sourceMetric.evidence_refs.length > 0 || (params.sourceMapEntry?.source_ids.length ?? 0) > 0 ? "Traced" : "Missing",
    format: params.config.format,
  };
}

function getRecordMetrics(record: BackendPnlExtractionRecord) {
  return metricFieldConfigs.flatMap((config) => {
    const metric = (record as unknown as Record<string, BackendMetricValue | undefined | null>)[config.field];
    if (!metric) {
      return [];
    }

    return [{ config, metric, label: config.label }];
  });
}

function getPeriodMetric(period: BackendResolvedPnlPeriod, field: string) {
  return (period as unknown as Record<string, BackendResolvedMetricValue | undefined | null>)[field] ?? null;
}

function deriveMetricValue(period: BackendResolvedPnlPeriod, field: string) {
  if (field === "gross_margin_pct") {
    if (period.gross_profit && period.revenue && period.revenue.value !== 0) {
      return {
        value: period.gross_profit.value / period.revenue.value,
        metric: period.gross_profit,
        rationale: "Derived as Gross Profit divided by Revenue in the backend workbook.",
      };
    }
  }

  if (field === "ebitda_margin_pct") {
    if (period.ebitda && period.revenue && period.revenue.value !== 0) {
      return {
        value: period.ebitda.value / period.revenue.value,
        metric: period.ebitda,
        rationale: "Derived as EBITDA divided by Revenue in the backend workbook.",
      };
    }
  }

  return { value: null, metric: null, rationale: null };
}

function classifyValidationIssue(issue: BackendValidationIssue): {
  issueClass: ReviewIssueClass;
  issueLevel: ExceptionItem["issueLevel"];
  blocksExport: boolean;
  severity: Severity;
  category: string;
  suggestedResolution: string;
} {
  if (issue.code === "missing_required_field" || issue.code === "formula_mismatch" || issue.code === "no_pnl_records" || issue.code === "empty_data_room") {
    return {
      issueClass: "real_core_blocker" as ReviewIssueClass,
      issueLevel: issue.code === "formula_mismatch" || issue.code === "missing_required_field" ? "metric" as const : "table" as const,
      blocksExport: true,
      severity: (issue.severity === "error" ? "High" : "Medium") as Severity,
      category: issue.code === "missing_required_field" ? "Missing core metric input" : issue.code === "formula_mismatch" ? "Formula mismatch" : "Extraction blocked",
      suggestedResolution:
        issue.code === "missing_required_field"
          ? "Provide the missing core source input before exporting the workbook."
          : issue.code === "formula_mismatch"
            ? "Resolve the reconciliation issue before relying on the exported workbook."
            : "Check the uploaded files and rerun the backend pipeline.",
    };
  }

  if (issue.code === "conflicting_values" || issue.code === "obvious_unit_issue") {
    return {
      issueClass: "non_blocking_mapping_bug" as ReviewIssueClass,
      issueLevel: "row" as const,
      blocksExport: false,
      severity: "Medium" as Severity,
      category: issue.code === "conflicting_values" ? "Conflicting values" : "Possible unit issue",
      suggestedResolution: "Review the conflicting source rows before relying on this line item.",
    };
  }

  return {
    issueClass: "table_warning" as ReviewIssueClass,
    issueLevel: "table" as const,
    blocksExport: false,
    severity: (issue.severity === "info" ? "Low" : "Medium") as Severity,
    category: issue.code.replaceAll("_", " "),
    suggestedResolution: "Review the warning if it matters for your current export.",
  };
}

function countSegmentsBySource(payload: BackendPilotRunPayload) {
  const counts = new Map<string, number>();
  for (const record of payload.extraction_bundle.records) {
    counts.set(record.source_id, (counts.get(record.source_id) ?? 0) + 1);
  }
  return counts;
}

function detectBackendTableType(record: BackendPnlExtractionRecord) {
  if (record.revenue || record.direct_costs || record.operating_expenses || record.ebitda) {
    return "Income Statement";
  }
  if (record.employee_count || record.customer_concentration_pct) {
    return "KPI Table";
  }
  return "Supporting Detail";
}

function averageConfidence(
  metrics: Array<{ metric: BackendMetricValue }>,
) {
  if (metrics.length === 0) {
    return 0;
  }

  return Math.round(
    metrics.reduce((total, entry) => total + entry.metric.confidence, 0) / metrics.length * 100,
  );
}

function resolveMappingStatus(metric: BackendMetricValue): MappingStatus {
  return metric.confidence >= 0.88 ? "Approved" : metric.confidence >= 0.7 ? "Pending" : "Needs Review";
}

function buildMappingReasoning(
  label: string,
  metric: BackendMetricValue,
  record: BackendPnlExtractionRecord,
) {
  const evidence = metric.evidence_refs[0];
  return evidence
    ? `${label} was extracted from ${evidence.locator_label} with ${Math.round(metric.confidence * 100)}% confidence.`
    : `${label} was extracted for ${record.period_label ?? record.period_key ?? "an undated period"}.`;
}

function buildRawLabel(label: string, metric: BackendMetricValue) {
  return metric.evidence_refs[0]?.quote?.split("|")[0]?.trim() || label;
}

function parseBackendLocator(sourceLocator: string) {
  const [sourceSheetName, cellOrRow] = sourceLocator.includes("!")
    ? sourceLocator.split("!")
    : ["Imported file", sourceLocator];

  return {
    sourceSheetName: sourceSheetName || "Imported file",
    sourceLocation: sourceLocator,
    cellOrRow: cellOrRow || sourceLocator,
  };
}

function parseBackendNumericValue(rawValue: string) {
  const normalized = rawValue.replaceAll(",", "").trim().toLowerCase();
  const multiplier = normalized.endsWith("m")
    ? 1_000_000
    : normalized.endsWith("k")
      ? 1_000
      : 1;
  const numeric = Number(normalized.replaceAll(/[^0-9.-]+/g, ""));

  if (Number.isNaN(numeric)) {
    return null;
  }

  if (normalized.includes("%")) {
    return Math.abs(numeric) > 1 ? numeric / 100 : numeric;
  }

  return numeric * multiplier;
}

function detectBackendUnit(rawValue: string, mappedCategory: string) {
  if (rawValue.includes("%")) {
    return "%";
  }

  if (mappedCategory === "Headcount") {
    return "count";
  }

  return "$";
}

function getDependencyCandidates(mappedCategory: string) {
  if (mappedCategory === "Gross Profit") {
    return ["Revenue", "COGS"];
  }
  if (mappedCategory === "EBITDA") {
    return ["Gross Profit", "Operating Expenses"];
  }
  return [];
}

function mapCategoryToOutputLineKey(mappedCategory: string) {
  switch (mappedCategory) {
    case "Revenue":
      return "revenue";
    case "COGS":
      return "cogs";
    case "Gross Profit":
      return "gross_profit";
    case "Operating Expenses":
      return "operating_expenses";
    case "EBITDA":
      return "ebitda";
    case "Headcount":
      return "headcount";
    default:
      return slugify(mappedCategory || unmappedCategory);
  }
}

function mapCategoryToFormulaTemplateKey(mappedCategory: string) {
  switch (mappedCategory) {
    case "Revenue":
      return "revenue" as const;
    case "COGS":
      return "cogs" as const;
    case "Gross Profit":
      return "gross_profit" as const;
    case "Operating Expenses":
      return "operating_expenses" as const;
    case "EBITDA":
      return "ebitda" as const;
    case "Headcount":
      return "headcount" as const;
    default:
      return "none" as const;
  }
}

function formatBackendMetricValue(value: number | null, format: DatabookMetricRecord["format"]) {
  if (value === null) {
    return "Unavailable";
  }

  if (format === "percentage") {
    // Derived margins arrive as fractions (0.45 → 45%), but provided percent
    // metrics can arrive already expressed in points (45 → 45%). Multiplying
    // blindly rendered those as "4500.0%".
    const percent = Math.abs(value) > 1.5 ? value : value * 100;
    return `${percent.toFixed(1)}%`;
  }

  if (format === "number") {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatCanonicalMetricValue(metric: FinalMetricRecord) {
  if (metric.finalValue === null) {
    return "";
  }

  if (metric.unit === "%") {
    const percent = Math.abs(metric.finalValue) > 1.5 ? metric.finalValue : metric.finalValue * 100;
    return `${percent.toFixed(1)}%`;
  }

  if (metric.unit === "count") {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(metric.finalValue);
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(metric.finalValue);
}

function fallbackFormulaLabel(lineItemCode: string) {
  switch (lineItemCode) {
    case "gross_profit":
      return "=Revenue-COGS";
    case "gross_margin_pct":
      return "=Gross Profit/Revenue";
    case "ebitda":
      return "=Gross Profit-Operating Expenses";
    case "ebitda_margin_pct":
      return "=EBITDA/Revenue";
    default:
      return "Source-backed input";
  }
}

function humanizeMetricField(field: string) {
  switch (field) {
    case "direct_costs":
      return "COGS";
    case "operating_expenses":
      return "Operating Expenses";
    case "employee_count":
      return "Headcount";
    default:
      return field
        .replaceAll("_", " ")
        .replace(/\b\w/g, (match) => match.toUpperCase());
  }
}

function inferCategory(fileName: string): Deal["sourceFiles"][number]["detectedCategory"] {
  const normalized = fileName.toLowerCase();

  if (normalized.includes("customer") || normalized.includes("arr") || normalized.includes("churn")) {
    return "Customer Data";
  }

  if (normalized.includes("kpi") || normalized.includes("board") || normalized.includes("operating")) {
    return "KPI Reports";
  }

  if (normalized.includes("legal") || normalized.includes("doc")) {
    return "Legal / Misc";
  }

  return "Financials";
}

function buildArtifactPath(runId: string, artifactKey: string) {
  return `${BACKEND_PROXY_BASE}/${runId}/artifacts/${artifactKey}`;
}

function slugify(value: string) {
  return value
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-|-$/g, "")
    .slice(0, 60);
}

export function buildBackendRunSubtitle(deal: Deal) {
  if (!deal.backendRun) {
    return null;
  }

  return `Processed by the Python pipeline on ${formatDateTime(deal.recentActivity[0]?.timestamp ?? new Date().toISOString())}.`;
}
