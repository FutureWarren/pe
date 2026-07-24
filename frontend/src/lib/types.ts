export type DealStatus =
  | "Intake"
  | "Initial Scan"
  | "Extraction"
  | "Mapping"
  | "Review"
  | "Output Ready";

export type FileCategory =
  | "Financials"
  | "KPI Reports"
  | "Customer Data"
  | "Legal / Misc";

export type FileStatus = "Connected" | "Indexed" | "Flagged" | "Scanning";

export type MappingStatus =
  | "Approved"
  | "Needs Review"
  | "Pending"
  | "Rule Applied";

export type Severity = "Critical" | "High" | "Medium" | "Low";

export type ExceptionStatus = "Open" | "Approved" | "Deferred" | "Edited";

export type OutputStatus =
  | "Ready"
  | "Needs Review"
  | "Queued"
  | "In Progress";

export type ReviewItemOrigin = "manual" | "generated";
export type DirectOrDerived = "Direct" | "Derived";
export type TraceabilityStatus = "Traced" | "Partial" | "Missing";
export type RowRoutingBucket =
  | "Core Financial"
  | "KPI Input"
  | "Duplicate Candidate"
  | "Supporting Detail"
  | "Customer Detail"
  | "Noise";
export type ReviewScope = "core" | "supporting";
export type ReviewIssueLevel = "metric" | "row" | "table";
export type ReviewIssueClass =
  | "real_core_blocker"
  | "non_blocking_mapping_bug"
  | "kpi_scope_issue"
  | "table_warning";
export type FormulaRole = "Input" | "Reported Metric" | "Derived Candidate" | "Review";
export type FormulaTemplateKey =
  | "revenue"
  | "cogs"
  | "gross_profit"
  | "gross_margin"
  | "operating_expenses"
  | "ebitda"
  | "ebitda_margin"
  | "arr"
  | "net_revenue_retention"
  | "customer_churn"
  | "headcount"
  | "capex"
  | "none";
export type CalculationType =
  | "Source Reported"
  | "Aggregation"
  | "Formula"
  | "Ratio"
  | "Manual Review";

export interface DefinedItem {
  id: string;
  sourceFileId: string;
  sourceFileName: string;
  sourceSheetName: string;
  sourceLocation: string;
  period: string;
  rawLabel: string;
  rawValue: string;
  normalizedValue: number | null;
  unit: string;
  detectedType: string;
  mappedCategory: string;
  mappedMetric: string;
  outputLineKey: string;
  formulaRole: FormulaRole;
  dependencyCandidates: string[];
  formulaTemplateKey: FormulaTemplateKey;
  definition: string;
  rationale: string;
  calculationType: CalculationType;
  directOrDerived: DirectOrDerived;
  formulaDependencies: string[];
  reviewStatus: MappingStatus | "Flagged";
  traceabilityStatus: TraceabilityStatus;
  routingBucket?: RowRoutingBucket;
  entersCorePipeline?: boolean;
  routingReason?: string;
}

export interface FormulaInputAssignment {
  id: string;
  definedItemId: string;
  sourceFileId: string;
  sourceFileName: string;
  sourceSheetName: string;
  sourceLocation: string;
  rawLabel: string;
  rawValue: string;
  period: string;
  mappedMetric: string;
  outputLineKey: string;
  formulaRole: FormulaRole;
  dependencyCandidates: string[];
  formulaTemplateKey: FormulaTemplateKey;
  directOrDerived: DirectOrDerived;
  normalizedValue: number | null;
  assignedValue: number | null;
  unit: string;
  rationale: string;
  reviewStatus: MappingStatus | "Flagged";
  traceabilityStatus: TraceabilityStatus;
}

export interface DatabookMetricRecord {
  key: string;
  outputLineKey: string;
  label: string;
  period: string;
  value: number | null;
  formattedValue: string;
  status: "Provided" | "Calculated" | "Unavailable";
  formula: string;
  definition: string;
  rationale: string;
  calculationType: CalculationType;
  directOrDerived: DirectOrDerived;
  formulaDependencies: string[];
  sourceInputIds: string[];
  sourceSummary: string;
  traceabilityStatus: TraceabilityStatus;
  format: "currency" | "percentage" | "number";
}

export interface TraceabilityRecord {
  id: string;
  outputMetricKey: string;
  outputLineKey: string;
  outputMetricLabel: string;
  outputMetricValue: string;
  sourceInputId?: string;
  definedItemId?: string;
  sourceFileId?: string;
  sourceFileName: string;
  sourceSheetName: string;
  sourceLocation: string;
  rawLabel: string;
  rawValue: string;
  period: string;
  mappedCategory: string;
  directOrDerived: DirectOrDerived;
  formulaDependencies: string[];
  derivationPath: string;
  traceabilityStatus: TraceabilityStatus;
}

export interface ExtractedDataRow {
  label: string;
  value: string;
  location: string;
  rawCells: string[];
}

export interface SourceFile {
  id: string;
  name: string;
  fileType: string;
  uploadDate: string;
  detectedCategory: FileCategory;
  status: FileStatus;
  /** Real page/sheet count when known; omit rather than fabricate. */
  pages?: number;
  owner: string;
  supportedForParsing?: boolean;
}

export interface ExtractedItem {
  id: string;
  title: string;
  sourceFileId: string;
  period: string;
  confidence: number;
  detectedTableType: string;
  issueFlags: string[];
  summary: string;
  tableName?: string;
  rows?: ExtractedDataRow[];
  routingBucket?: RowRoutingBucket;
  coreRowCount?: number;
  supportingRowCount?: number;
}

export interface MappingRow {
  id: string;
  sourceFileId: string;
  sourceLocator: string;
  rawLineItemLabel: string;
  rawValue: string;
  period: string;
  mappedCategory: string;
  confidence: number;
  sourceLinked: boolean;
  status: MappingStatus;
  reasoning: string;
  definitionHint?: string;
  directOrDerivedHint?: DirectOrDerived;
  dependencyCandidatesHint?: string[];
  interpretationProvider?: "deterministic" | "gemini";
  routingBucket?: RowRoutingBucket;
  entersCorePipeline?: boolean;
  routingReason?: string;
  duplicateGroupKey?: string;
  duplicateRole?: "primary" | "collapsed";
  duplicateConflict?: boolean;
}

export interface ExceptionItem {
  id: string;
  issueKey?: string;
  origin?: ReviewItemOrigin;
  scope?: ReviewScope;
  blocksExport?: boolean;
  issueLevel?: ReviewIssueLevel;
  issueClass?: ReviewIssueClass;
  mappingRowId?: string;
  extractedItemId?: string;
  sourceFileId?: string;
  severity: Severity;
  category: string;
  affectedLineItem: string;
  suggestedResolution: string;
  assignedOwner: string;
  status: ExceptionStatus;
  detail: string;
}

export interface OutputPreviewTableRow {
  item: string;
  valueA: string;
  valueB?: string;
  trace: string;
}

export interface OutputPreviewSection {
  heading: string;
  bullets: string[];
}

export interface OutputAsset {
  id: string;
  name: string;
  status: OutputStatus;
  generatedDate: string;
  completeness: number;
  sourceLinked: boolean;
  reviewStatus: string;
  rowCount?: number;
  previewType: "table" | "sections";
  previewRows?: OutputPreviewTableRow[];
  previewSections?: OutputPreviewSection[];
}

export interface BackendRunInfo {
  runId: string;
  extractionBackend: "deterministic" | "gemini";
  workbookDownloadPath: string;
  validationReportPath?: string;
  resolvedPnlPath?: string;
  extractedPnlPath?: string;
  sourceMapPath?: string;
  validationStatus?: "pass" | "warning" | "fail";
  issueCount?: number;
}

export interface RecentActivityItem {
  id: string;
  title: string;
  description: string;
  timestamp: string;
}

export interface CopilotPrompt {
  id: string;
  label: string;
  response: string;
}

export type AnalystConfidenceLevel = "High" | "Medium" | "Low";
export type AnalystValidationResult = "Matched" | "Formula" | "Single-source" | "Mismatch";
export type AnalystMetricStatus = "Ready" | "Review";
export type AnalystExceptionSeverity = "Info" | "Review" | "Critical";
export type AnalystDirectOrDerived = "direct" | "derived";
export type AnalystMetricUnit = "USD" | "USD_thousands" | "%" | "count" | "months" | "ratio";
export type AnalystExplainQuestion =
  | "source"
  | "why_this_source"
  | "direct_or_derived"
  | "cross_checks"
  | "confidence"
  | "where_to_verify"
  | "compare_files"
  | "summary";

export interface AnalystSourceCitation {
  file: string;
  tab?: string | null;
  range?: string | null;
  value?: number | null;
  sourceId?: string | null;
}

export interface FinalMetricRecord {
  metricKey: string;
  metricName: string;
  period: string;
  periodKey: string;
  periodOrder: number;
  finalValue: number | null;
  unit: AnalystMetricUnit | null;
  selectedSource?: AnalystSourceCitation | null;
  backupSources: AnalystSourceCitation[];
  sourcePriorityReason?: string | null;
  directOrDerived: AnalystDirectOrDerived;
  derivationFormula?: string | null;
  validationResult: AnalystValidationResult;
  confidenceLevel: AnalystConfidenceLevel;
  confidenceReason: string;
  status: AnalystMetricStatus;
  note?: string | null;
  crossCheckLog: string[];
}

export interface AnalystExceptionRow {
  metric: string;
  period: string;
  issue: string;
  systemView: string;
  suggestedAction: string;
  severity: AnalystExceptionSeverity;
  relatedMetricKey?: string | null;
  relatedPeriodKey?: string | null;
}

export interface AnalystBundle {
  metrics: FinalMetricRecord[];
  exceptions: AnalystExceptionRow[];
  periodOrder: string[];
  periodKeys: string[];
  metricOrder: string[];
}

export interface AnalystExplainResponse {
  answer: string;
  metricKey: string;
  periodKey: string;
  question: AnalystExplainQuestion;
  confidenceLevel?: AnalystConfidenceLevel | null;
  status?: AnalystMetricStatus | null;
  selectedSourceFile?: string | null;
  selectedSourceTab?: string | null;
  selectedSourceRange?: string | null;
}

export interface Deal {
  id: string;
  targetCompanyName: string;
  sector: string;
  status: DealStatus;
  outputStatus?: string;
  sourceFilesConnected: boolean;
  sourceFileIds?: string[];
  extractionProgress: number;
  exceptionCount: number;
  outputsReady: boolean;
  seller: string;
  sponsor: string;
  geography: string;
  stage: string;
  enterpriseValue: number;
  ttmRevenue: number;
  ttmEbitda: number;
  readinessScore: number;
  workflowProgress: number;
  recentActivity: RecentActivityItem[];
  sourceFiles: SourceFile[];
  extractedItems: ExtractedItem[];
  mappingRows: MappingRow[];
  definedItems?: DefinedItem[];
  formulaInputs?: FormulaInputAssignment[];
  databookMetrics?: DatabookMetricRecord[];
  traceabilityRecords?: TraceabilityRecord[];
  exceptions: ExceptionItem[];
  outputs: OutputAsset[];
  analystBundle?: AnalystBundle;
  copilotPrompts: CopilotPrompt[];
  processingEngine?: "frontend_local" | "backend_python";
  backendRun?: BackendRunInfo;
  qualityPanel: {
    missingHeaders: number;
    duplicateFiles: number;
    unreadablePages: number;
    unitAmbiguity: number;
    confidenceSummary: {
      high: number;
      medium: number;
      low: number;
    };
  };
}

export interface UploadTemplate {
  id: string;
  name: string;
  fileType: string;
  detectedCategory: FileCategory;
  status: FileStatus;
}
