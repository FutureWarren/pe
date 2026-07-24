import * as XLSX from "xlsx";

import { buildDefinedItems } from "@/lib/definition-engine";
import {
  detectUnit,
  dedupeStrings,
  isNumericLike,
  normalizeLabel,
  normalizeValueForUnit,
  parseSourceLocator,
  parseScaledFinancialValue,
} from "@/lib/dataroom-utils";
import { buildDatabookMetricsFromFormulaInputs, getDatabookReadiness } from "@/lib/formula-engine";
import { buildFormulaInputAssignments } from "@/lib/formula-input-assignment";
import { standardTags } from "@/lib/mock-data";
import { buildTraceabilityRecords } from "@/lib/traceability";
import {
  DatabookMetricRecord,
  Deal,
  DealStatus,
  DefinedItem,
  ExceptionItem,
  ExtractedDataRow,
  ExtractedItem,
  FileCategory,
  FileStatus,
  MappingRow,
  MappingStatus,
  OutputAsset,
  OutputPreviewSection,
  OutputPreviewTableRow,
  OutputStatus,
  ReviewScope,
  RowRoutingBucket,
  ReviewIssueLevel,
  ReviewIssueClass,
  Severity,
  SourceFile,
} from "@/lib/types";
import { getWorkflowSnapshot } from "@/lib/workflow";
import { isBlockingCoreIssue, isNonBlockingRowIssue, isTableWarning } from "@/lib/review-utils";

export const LOCAL_STORAGE_DEALS_KEY = "angelic-local-deals-v1";
export const unmappedCategory = "Unmapped";
export const mappingTagOptions = [unmappedCategory, ...standardTags];

export interface IntakeUploadInput {
  file?: File;
  name: string;
  fileType: string;
  detectedCategory: FileCategory;
  uploadDate?: string;
  status?: FileStatus;
}

export interface IntakeScanSummary {
  fileCount: number;
  financialTables: number;
  possibleIssues: number;
  readinessScore: number;
}

export interface IntakeScanResult {
  sourceFiles: SourceFile[];
  extractedItems: ExtractedItem[];
  mappingRows: MappingRow[];
  scanSummary: IntakeScanSummary;
}

const periodPattern =
  /(fy\s?\d{2,4}[a-z]?|q[1-4]\s?\d{2,4}|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|\d{4})/i;

const categoryHeuristics: Array<{
  category: string;
  keywords: string[];
  exact?: string[];
}> = [
  {
    category: "Net Revenue Retention",
    keywords: [
      "net revenue retention",
      "nrr",
      "net dollar retention",
      "gross revenue retention",
      "grr",
      "retention",
    ],
    exact: [
      "net revenue retention",
      "nrr",
      "net dollar retention",
      "gross revenue retention",
      "grr",
    ],
  },
  {
    category: "Revenue",
    keywords: [
      "revenue",
      "sales",
      "subscription revenue",
      "services revenue",
      "recurring revenue",
      "turnover",
    ],
    exact: ["revenue", "total revenue", "net revenue", "subscription revenue", "services revenue"],
  },
  {
    category: "COGS",
    keywords: [
      "cogs",
      "cost of goods sold",
      "cost of goods",
      "cost of revenue",
      "cost of sales",
      "hosting",
      "cloud infrastructure",
      "delivery labor",
      "implementation labor",
      "merchant fees",
      "support labor",
    ],
    exact: ["cogs", "cost of revenue", "cost of sales", "cost of goods sold"],
  },
  {
    category: "Gross Profit",
    keywords: ["gross profit", "gross margin $", "gross margin dollars"],
    exact: ["gross profit"],
  },
  {
    category: "Operating Expenses",
    keywords: [
      "operating expenses",
      "opex",
      "sales and marketing",
      "sales marketing",
      "s m",
      "g a",
      "general and administrative",
      "general administrative",
      "research and development",
      "research development",
      "r d",
      "sg a",
    ],
    exact: ["operating expenses", "opex", "sales marketing", "s m", "g a", "r d", "sg a"],
  },
  {
    category: "EBITDA",
    keywords: ["ebitda", "adjusted ebitda"],
    exact: ["ebitda", "adjusted ebitda"],
  },
  {
    category: "ARR",
    keywords: ["arr", "annual recurring revenue"],
    exact: ["arr", "annual recurring revenue"],
  },
  {
    category: "Customer Churn",
    keywords: ["churn", "logo churn", "revenue churn", "customer churn"],
    exact: ["customer churn", "logo churn", "revenue churn"],
  },
  {
    category: "Headcount",
    keywords: ["headcount", "fte", "employee count", "total employees", "total headcount", "workforce"],
    exact: ["headcount", "total headcount", "employee count", "total employees", "total fte", "fte"],
  },
  {
    category: "CapEx",
    keywords: ["capex", "capital expenditure", "capital expenditures"],
    exact: ["capex", "capital expenditures"],
  },
];

const kpiCategories = new Set([
  "ARR",
  "Net Revenue Retention",
  "Customer Churn",
  "Headcount",
  "CapEx",
]);
const coreBlockingCategories = new Set(["Revenue", "COGS", "Operating Expenses"]);
const customerDetailTableKeywords = [
  "customer concentration",
  "top customers",
  "customer detail",
  "billing export",
  "billing detail",
  "invoice detail",
  "accounts receivable",
  "ar aging",
  "customer listing",
  "by customer",
];
const supportingDetailTableKeywords = [
  "detail",
  "appendix",
  "bridge",
  "department",
  "by department",
  "segment",
  "product",
  "cohort",
  "vendor",
  "supporting schedule",
];
const noiseLabelKeywords = [
  "id",
  "invoice number",
  "account number",
  "customer id",
  "reference number",
];
const companyNameSuffixes = [
  "inc",
  "llc",
  "ltd",
  "lp",
  "plumbing",
  "mechanical",
  "care",
  "group",
  "services",
  "solutions",
  "systems",
  "partners",
  "holdings",
  "facility",
];

type MappingMatchStrength = "exact" | "label" | "context" | "none";

interface MappingClassification {
  mappedCategory: string;
  confidence: number;
  status: MappingStatus;
  reasoning: string;
  matchStrength: MappingMatchStrength;
}

interface RowRoutingDecision {
  routingBucket: RowRoutingBucket;
  entersCorePipeline: boolean;
  routingReason: string;
}

function includesKeyword(haystack: string, keywords: string[]) {
  const normalizedHaystack = normalizeLabel(haystack);

  return keywords.some((keyword) => normalizedHaystack.includes(normalizeLabel(keyword)));
}

function looksLikeNamedDetailRow(label: string) {
  const trimmed = label.trim();

  if (trimmed.length < 6 || trimmed.length > 80 || /[%$]/.test(trimmed) || /\d/.test(trimmed)) {
    return false;
  }

  const tokens = trimmed.split(/\s+/);

  if (tokens.length < 2 || tokens.length > 6) {
    return false;
  }

  const titleCaseTokens = tokens.filter((token) => /^[A-Z][A-Za-z&'.-]*$/.test(token)).length;
  const suffixMatch = tokens.some((token) =>
    companyNameSuffixes.includes(token.replaceAll(/[^a-z]/gi, "").toLowerCase()),
  );

  return suffixMatch || titleCaseTokens >= Math.max(2, tokens.length - 1);
}

function routeExtractedRow(params: {
  label: string;
  rawValue: string;
  rawCells: string[];
  tableName: string;
  classification: MappingClassification;
}): RowRoutingDecision {
  const normalizedLabel = normalizeLabel(params.label);
  const normalizedTableName = normalizeLabel(params.tableName);
  const normalizedContext = normalizeLabel(
    `${params.tableName} ${params.label} ${params.rawCells.join(" ")}`,
  );
  const mappedCategory = params.classification.mappedCategory;
  const isLikelyCustomerDetail =
    includesKeyword(normalizedContext, customerDetailTableKeywords) ||
    looksLikeNamedDetailRow(params.label);
  const isNoiseLike =
    includesKeyword(normalizedLabel, noiseLabelKeywords) ||
    normalizedLabel === "name" ||
    normalizedLabel === "customer";
  const isSupportingTable = includesKeyword(normalizedTableName, supportingDetailTableKeywords);
  const isCoreStatementTable = includesKeyword(normalizedTableName, ["p l", "income statement", "qoe"]);
  const isKpiTable = includesKeyword(normalizedTableName, ["operating kpis", "kpi", "headcount", "arr", "churn", "capex"]);

  if (isNoiseLike) {
    return {
      routingBucket: "Noise" as RowRoutingBucket,
      entersCorePipeline: false,
      routingReason:
        "The row behaves like metadata or an identifier, not a databook metric input.",
    };
  }

  if (isLikelyCustomerDetail) {
    return {
      routingBucket: "Customer Detail" as RowRoutingBucket,
      entersCorePipeline: false,
      routingReason:
        "The row looks like customer-level or billing detail rather than a standard databook input.",
    };
  }

  if (mappedCategory !== unmappedCategory) {
    const isKpiMetric = kpiCategories.has(mappedCategory);
    const hasStrongMetricSignal =
      params.classification.matchStrength === "exact" ||
      params.classification.matchStrength === "label";

    if (isSupportingTable && !hasStrongMetricSignal) {
      return {
        routingBucket: "Supporting Detail" as RowRoutingBucket,
        entersCorePipeline: false,
        routingReason:
          "The row sits in a supporting schedule and only showed a weak contextual match, so it was held out of the core databook path.",
      };
    }

    if (isKpiMetric && !isKpiTable && params.classification.matchStrength === "context") {
      return {
        routingBucket: "Supporting Detail" as RowRoutingBucket,
        entersCorePipeline: false,
        routingReason:
          "The KPI match depended on weak context rather than a strong KPI label, so it was not promoted into the core databook path.",
      };
    }

    if (!isKpiMetric && !isCoreStatementTable && isSupportingTable && !hasStrongMetricSignal) {
      return {
        routingBucket: "Supporting Detail" as RowRoutingBucket,
        entersCorePipeline: false,
        routingReason:
          "The row came from a supporting schedule without a strong core financial label, so it was kept out of the main databook flow.",
      };
    }

    return {
      routingBucket: (isKpiMetric
        ? "KPI Input"
        : "Core Financial") as RowRoutingBucket,
      entersCorePipeline: true,
      routingReason: `${mappedCategory} matched the standard databook taxonomy strongly enough to enter the core flow.`,
    };
  }

  if (isSupportingTable || isCoreStatementTable || isKpiTable) {
    return {
      routingBucket: "Supporting Detail" as RowRoutingBucket,
      entersCorePipeline: false,
      routingReason:
        "The row sits in a supporting schedule or detail table without a strong databook metric signal.",
    };
  }

  return {
    routingBucket: "Supporting Detail" as RowRoutingBucket,
    entersCorePipeline: false,
    routingReason:
      "The row was retained as supporting detail because it does not cleanly belong in the core databook taxonomy.",
  };
}

function isCoreBlockingMappingRow(row: MappingRow) {
  if (row.entersCorePipeline === false) {
    return false;
  }

  if (coreBlockingCategories.has(row.mappedCategory)) {
    return true;
  }

  const normalizedLabel = normalizeLabel(row.rawLineItemLabel);

  return [
    "revenue",
    "sales",
    "cost of revenue",
    "cost of goods",
    "cost of sales",
    "cogs",
    "operating expenses",
    "opex",
    "sales marketing",
    "general administrative",
    "research development",
  ].some((keyword) => normalizedLabel.includes(normalizeLabel(keyword)));
}

function buildRoutingAwareMappingRow(params: {
  sourceFileId: string;
  period: string;
  row: ExtractedDataRow;
  tableName: string;
}) {
  const classification = classifyMapping(params.row.label, params.tableName);
  const routing = routeExtractedRow({
    label: params.row.label,
    rawValue: params.row.value,
    rawCells: params.row.rawCells,
    tableName: params.tableName,
    classification,
  });

  if (!routing.entersCorePipeline) {
    return {
      id: createId("map"),
      sourceFileId: params.sourceFileId,
      sourceLocator: params.row.location,
      rawLineItemLabel: params.row.label,
      rawValue: params.row.value,
      period: params.period,
      mappedCategory: unmappedCategory,
      confidence: 92,
      sourceLinked: true,
      status: "Rule Applied" as MappingStatus,
      reasoning: routing.routingReason,
      interpretationProvider: "deterministic" as const,
      routingBucket: routing.routingBucket,
      entersCorePipeline: false,
      routingReason: routing.routingReason,
    } satisfies MappingRow;
  }

  return {
    id: createId("map"),
    sourceFileId: params.sourceFileId,
    sourceLocator: params.row.location,
    rawLineItemLabel: params.row.label,
    rawValue: params.row.value,
    period: params.period,
    mappedCategory: classification.mappedCategory,
    confidence: classification.confidence,
    sourceLinked: true,
    status: classification.status,
    reasoning: classification.reasoning,
    interpretationProvider: "deterministic" as const,
    routingBucket: routing.routingBucket,
    entersCorePipeline: true,
    routingReason: routing.routingReason,
  } satisfies MappingRow;
}

function getMappingStatusRank(status: MappingStatus) {
  if (status === "Approved") {
    return 3;
  }

  if (status === "Pending") {
    return 2;
  }

  if (status === "Needs Review") {
    return 1;
  }

  return 0;
}

function getRowComparableValue(row: MappingRow) {
  const unit = detectUnit({
    rawValue: row.rawValue,
    rawLabel: row.rawLineItemLabel,
    mappedCategory: row.mappedCategory,
  });

  return normalizeValueForUnit(row.rawValue, unit) ?? parseScaledFinancialValue(row.rawValue);
}

function buildDuplicateGroupKey(row: MappingRow) {
  return `${normalizeLabel(row.rawLineItemLabel)}|${normalizeLabel(row.period)}`;
}

function collapseDuplicateCandidates(mappingRows: MappingRow[]) {
  const rows = [...mappingRows];
  const duplicateGroups = new Map<string, number[]>();

  rows.forEach((row, index) => {
    if (row.entersCorePipeline === false) {
      return;
    }

    const normalizedLabel = normalizeLabel(row.rawLineItemLabel);
    if (!normalizedLabel) {
      return;
    }

    const key = buildDuplicateGroupKey(row);
    duplicateGroups.set(key, [...(duplicateGroups.get(key) ?? []), index]);
  });

  duplicateGroups.forEach((indexes, groupKey) => {
    if (indexes.length < 2) {
      return;
    }

    const rankedIndexes = [...indexes].sort((leftIndex, rightIndex) => {
      const left = rows[leftIndex];
      const right = rows[rightIndex];

      return (
        getMappingStatusRank(right.status) - getMappingStatusRank(left.status) ||
        right.confidence - left.confidence
      );
    });
    const primaryIndex = rankedIndexes[0];
    const duplicateRows = rankedIndexes.map((index) => rows[index]);
    const comparableValues = duplicateRows
      .map((row) => getRowComparableValue(row))
      .filter((value): value is number => value !== null);
    const categorySet = new Set(duplicateRows.map((row) => row.mappedCategory));
    const duplicateConflict =
      categorySet.size > 1 ||
      comparableValues.some((value, index) =>
        comparableValues.slice(index + 1).some((other) => valuesConflict(value, other)),
      );

    rows[primaryIndex] = {
      ...rows[primaryIndex],
      duplicateGroupKey: groupKey,
      duplicateRole: "primary",
      duplicateConflict,
    };

    rankedIndexes.slice(1).forEach((index) => {
      rows[index] = {
        ...rows[index],
        duplicateGroupKey: groupKey,
        duplicateRole: "collapsed",
        duplicateConflict,
        entersCorePipeline: false,
        routingBucket: "Duplicate Candidate",
        status: "Rule Applied",
        confidence: Math.max(rows[index].confidence, 94),
        routingReason: duplicateConflict
          ? "The row was collapsed into a duplicate candidate set so one primary row can stay in the core databook path while the conflict is reviewed."
          : "The row was collapsed as an obvious duplicate candidate before Gemini and core databook assignment.",
        reasoning: duplicateConflict
          ? "Duplicate candidate values were held out of the core path pending manual confirmation."
          : "Duplicate candidate was collapsed before core mapping to keep the clean batch quiet.",
      };
    });
  });

  return rows;
}

function summarizeDuplicateRouting(mappingRows: MappingRow[]) {
  const collapsedDuplicates = mappingRows.filter(
    (row) => row.routingBucket === "Duplicate Candidate" && row.duplicateRole === "collapsed",
  ).length;
  const conflictingDuplicateGroups = new Set(
    mappingRows
      .filter((row) => row.duplicateRole === "primary" && row.duplicateConflict)
      .map((row) => row.duplicateGroupKey)
      .filter((value): value is string => Boolean(value)),
  ).size;

  return {
    collapsedDuplicates,
    conflictingDuplicateGroups,
  };
}

function summarizeExtractedItemRouting(mappingRows: MappingRow[]) {
  const coreRowCount = mappingRows.filter((row) => row.entersCorePipeline !== false).length;
  const supportingRowCount = mappingRows.length - coreRowCount;
  const routingBucket =
    coreRowCount > 0
      ? mappingRows.some((row) => row.routingBucket === "Core Financial")
        ? "Core Financial"
        : "KPI Input"
      : mappingRows.some((row) => row.routingBucket === "Duplicate Candidate")
        ? "Duplicate Candidate"
      : mappingRows.some((row) => row.routingBucket === "Customer Detail")
        ? "Customer Detail"
        : mappingRows.some((row) => row.routingBucket === "Supporting Detail")
          ? "Supporting Detail"
          : "Noise";

  return {
    routingBucket: routingBucket as RowRoutingBucket,
    coreRowCount,
    supportingRowCount,
  };
}

function ensureMappingRowRouting(row: MappingRow, extractedItems: ExtractedItem[]) {
  if (typeof row.entersCorePipeline === "boolean" && row.routingBucket) {
    return row;
  }

  if (row.mappedCategory !== unmappedCategory) {
    return {
      ...row,
      routingBucket: (kpiCategories.has(row.mappedCategory)
        ? "KPI Input"
        : "Core Financial") as RowRoutingBucket,
      entersCorePipeline: true,
      routingReason: row.routingReason ?? `${row.mappedCategory} is treated as a core databook metric input.`,
    };
  }

  const sourceInfo = parseSourceLocator(row.sourceLocator);
  const extractedItem = extractedItems.find(
    (item) =>
      item.sourceFileId === row.sourceFileId &&
      normalizeLabel(item.tableName ?? item.title) === normalizeLabel(sourceInfo.sourceSheetName),
  );
  const routing = routeExtractedRow({
    label: row.rawLineItemLabel,
    rawValue: row.rawValue,
    rawCells: [],
    tableName: extractedItem?.tableName ?? extractedItem?.title ?? sourceInfo.sourceSheetName,
    classification: {
      mappedCategory: row.mappedCategory,
      confidence: row.confidence,
      status: row.status,
      reasoning: row.reasoning,
      matchStrength: row.mappedCategory === unmappedCategory ? "none" : "label",
    },
  });

  return {
    ...row,
    mappedCategory: routing.entersCorePipeline ? row.mappedCategory : unmappedCategory,
    confidence: routing.entersCorePipeline ? row.confidence : Math.max(row.confidence, 92),
    status: routing.entersCorePipeline ? row.status : ("Rule Applied" as MappingStatus),
    reasoning: routing.entersCorePipeline ? row.reasoning : routing.routingReason,
    routingBucket: routing.routingBucket,
    entersCorePipeline: routing.entersCorePipeline,
    routingReason: routing.routingReason,
  };
}

type MappingGuardrailResult =
  | {
      kind: "remap";
      mappedCategory: string;
      routingBucket: RowRoutingBucket;
      entersCorePipeline: boolean;
      status: MappingStatus;
      confidence: number;
      reasoning: string;
    }
  | {
      kind: "flag";
      mappedCategory: string;
      routingBucket: RowRoutingBucket;
      entersCorePipeline: boolean;
      status: MappingStatus;
      confidence: number;
      reasoning: string;
    }
  | null;

function getValueTypeForGuardrail(row: MappingRow) {
  const unit = detectUnit({
    rawValue: row.rawValue,
    rawLabel: row.rawLineItemLabel,
  });

  if (unit === "%") {
    return "percentage" as const;
  }

  if (unit === "count") {
    return "count" as const;
  }

  if (unit === "USD") {
    return "currency" as const;
  }

  return "unknown" as const;
}

function inferLabelFamily(row: MappingRow) {
  const label = normalizeLabel(row.rawLineItemLabel);
  const context = normalizeLabel(`${row.rawLineItemLabel} ${row.sourceLocator}`);

  if (
    label.includes("net revenue retention") ||
    label === "nrr" ||
    label.includes("net dollar retention") ||
    label.includes("gross revenue retention") ||
    label === "grr" ||
    (label.includes("retention") && (label.includes("revenue") || context.includes("operating kpis")))
  ) {
    return "retention_kpi" as const;
  }

  if (label.includes("churn")) {
    return "churn_kpi" as const;
  }

  if (
    label.includes("capex") ||
    label.includes("capital expend") ||
    label.includes("capital invest") ||
    label.includes("capital expenditure")
  ) {
    return "capex" as const;
  }

  if (
    label.includes("headcount") ||
    label.includes("employee count") ||
    label.includes("employees") ||
    label.includes("fte") ||
    label.includes("workforce")
  ) {
    return "headcount" as const;
  }

  if (
    label.includes("revenue") ||
    label.includes("sales") ||
    label.includes("cogs") ||
    label.includes("cost of") ||
    label.includes("opex") ||
    label.includes("operating expenses") ||
    label.includes("ebitda") ||
    label.includes("gross profit")
  ) {
    return "core_currency" as const;
  }

  return "other" as const;
}

function applyDeterministicMappingGuardrail(row: MappingRow): MappingGuardrailResult {
  if (row.entersCorePipeline === false) {
    return null;
  }

  const valueType = getValueTypeForGuardrail(row);
  const labelFamily = inferLabelFamily(row);
  const mappedCategory = row.mappedCategory;
  const isCoreCurrencyMetric = ["Revenue", "COGS", "Operating Expenses", "Gross Profit", "EBITDA"].includes(
    mappedCategory,
  );

  if (labelFamily === "retention_kpi") {
    if (mappedCategory !== "Net Revenue Retention") {
      return {
        kind: "remap",
        mappedCategory: "Net Revenue Retention",
        routingBucket: "KPI Input",
        entersCorePipeline: true,
        status: "Rule Applied",
        confidence: Math.max(row.confidence, 94),
        reasoning:
          "Deterministic KPI guardrail re-routed this row into Net Revenue Retention because the label indicates a retention KPI, not a core Revenue input.",
      };
    }
  }

  if (labelFamily === "churn_kpi" && mappedCategory !== "Customer Churn") {
    return {
      kind: "remap",
      mappedCategory: "Customer Churn",
      routingBucket: "KPI Input",
      entersCorePipeline: true,
      status: "Rule Applied",
      confidence: Math.max(row.confidence, 92),
      reasoning:
        "Deterministic KPI guardrail re-routed this row into Customer Churn because the label and value indicate a churn ratio, not a core currency metric.",
    };
  }

  if (labelFamily === "capex" && mappedCategory !== "CapEx") {
    return {
      kind: "remap",
      mappedCategory: "CapEx",
      routingBucket: "KPI Input",
      entersCorePipeline: true,
      status: "Rule Applied",
      confidence: Math.max(row.confidence, 95),
      reasoning:
        "Deterministic family guardrail re-routed this row into CapEx because capital expenditure labels must not feed Headcount.",
    };
  }

  if (labelFamily === "headcount" && mappedCategory !== "Headcount" && valueType === "count") {
    return {
      kind: "remap",
      mappedCategory: "Headcount",
      routingBucket: "KPI Input",
      entersCorePipeline: true,
      status: "Rule Applied",
      confidence: Math.max(row.confidence, 92),
      reasoning:
        "Deterministic family guardrail re-routed this row into Headcount because the label and count-like value indicate a workforce KPI, not a currency metric.",
    };
  }

  if (mappedCategory === "Headcount" && valueType === "currency") {
    if (labelFamily === "capex") {
      return {
        kind: "remap",
        mappedCategory: "CapEx",
        routingBucket: "KPI Input",
        entersCorePipeline: true,
        status: "Rule Applied",
        confidence: Math.max(row.confidence, 95),
        reasoning:
          "Deterministic family guardrail re-routed this row into CapEx because the value is monetary and the label indicates capital expenditures, not headcount.",
      };
    }

    return {
      kind: "flag",
      mappedCategory: unmappedCategory,
      routingBucket: "Supporting Detail",
      entersCorePipeline: false,
      status: "Needs Review",
      confidence: Math.min(row.confidence, 36),
      reasoning:
        "Deterministic family guardrail removed this row from Headcount because monetary values cannot be accepted as employee counts.",
    };
  }

  if (isCoreCurrencyMetric && valueType === "count" && labelFamily === "headcount") {
    return {
      kind: "remap",
      mappedCategory: "Headcount",
      routingBucket: "KPI Input",
      entersCorePipeline: true,
      status: "Rule Applied",
      confidence: Math.max(row.confidence, 92),
      reasoning:
        "Deterministic family guardrail re-routed this count row into Headcount because workforce counts must not stay in core currency metrics.",
    };
  }

  if (isCoreCurrencyMetric && valueType === "percentage") {
    if (labelFamily === "retention_kpi") {
      return {
        kind: "remap",
        mappedCategory: "Net Revenue Retention",
        routingBucket: "KPI Input",
        entersCorePipeline: true,
        status: "Rule Applied",
        confidence: Math.max(row.confidence, 94),
        reasoning:
          "Deterministic KPI guardrail re-routed this percentage row into Net Revenue Retention instead of a core currency metric.",
      };
    }

    if (labelFamily === "churn_kpi") {
      return {
        kind: "remap",
        mappedCategory: "Customer Churn",
        routingBucket: "KPI Input",
        entersCorePipeline: true,
        status: "Rule Applied",
        confidence: Math.max(row.confidence, 92),
        reasoning:
          "Deterministic KPI guardrail re-routed this percentage row into Customer Churn instead of a core currency metric.",
      };
    }

    return {
      kind: "flag",
      mappedCategory: unmappedCategory,
      routingBucket: "Supporting Detail",
      entersCorePipeline: false,
      status: "Needs Review",
      confidence: Math.min(row.confidence, 40),
      reasoning:
        "Deterministic type guardrail held this row out of the core workbook path because percentage values cannot feed currency metrics.",
    };
  }

  return null;
}

function applyDeterministicMappingGuardrails(mappingRows: MappingRow[]) {
  return mappingRows.map((row) => {
    const guardrail = applyDeterministicMappingGuardrail(row);

    if (!guardrail) {
      return row;
    }

    return {
      ...row,
      mappedCategory: guardrail.mappedCategory,
      routingBucket: guardrail.routingBucket,
      entersCorePipeline: guardrail.entersCorePipeline,
      status: guardrail.status,
      confidence: guardrail.confidence,
      reasoning: guardrail.reasoning,
      routingReason:
        guardrail.kind === "remap"
          ? `Post-Gemini deterministic guardrail kept the row in the ${guardrail.routingBucket.toLowerCase()} path with a safer metric family assignment.`
          : "Post-Gemini deterministic guardrail removed the row from the core workbook path because the metric family and value type do not agree.",
      interpretationProvider:
        row.interpretationProvider === "gemini" ? "deterministic" : row.interpretationProvider,
      definitionHint: undefined,
      directOrDerivedHint: undefined,
      dependencyCandidatesHint: [],
    } satisfies MappingRow;
  });
}

function createId(prefix: string) {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

function slugify(value: string) {
  return value
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-|-$/g, "")
    .slice(0, 60);
}

function stripExtension(value: string) {
  return value.replace(/\.[^/.]+$/, "");
}

function getFileType(value: string) {
  return value.split(".").pop()?.toUpperCase() ?? "FILE";
}

function getFileExtension(value: string) {
  return value.split(".").pop()?.toLowerCase() ?? "";
}

export function isSupportedStructuredFile(value: string) {
  const extension = getFileExtension(value);

  return extension === "csv" || extension === "xlsx";
}

export function getDatabookMetrics(deal: Deal): DatabookMetricRecord[] {
  if (deal.databookMetrics && deal.databookMetrics.length > 0) {
    return deal.databookMetrics;
  }

  return buildDatabookMetricsFromFormulaInputs(deal.formulaInputs ?? []);
}

function isHeaderLike(row: string[]) {
  const textCells = row.filter((cell) => cell && !isNumericLike(cell));
  const keywords = ["period", "month", "value", "amount", "label", "metric", "account"];

  return textCells.some((cell) =>
    keywords.some((keyword) => cell.toLowerCase().includes(keyword)),
  );
}

function findHeaderRowIndex(rows: string[][]) {
  return rows.findIndex((row) => isHeaderLike(row));
}

function detectPeriod(value: string) {
  const match = value.match(periodPattern);

  return match?.[0]?.replace(/\s+/g, " ").trim() ?? "Current Period";
}

function columnLabel(columnIndex: number) {
  let currentIndex = columnIndex;
  let label = "";

  while (currentIndex > 0) {
    const remainder = (currentIndex - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    currentIndex = Math.floor((currentIndex - 1) / 26);
  }

  return label;
}

function detectTableType(tableName: string, rows: ExtractedDataRow[]) {
  const haystack = `${tableName} ${rows.map((row) => row.label).join(" ")}`.toLowerCase();

  if (haystack.includes("headcount") || haystack.includes("fte")) {
    return "Workforce";
  }

  if (haystack.includes("retention") || haystack.includes("nrr") || haystack.includes("grr")) {
    return "KPI Table";
  }

  if (haystack.includes("churn") || haystack.includes("customer")) {
    return "KPI Table";
  }

  if (haystack.includes("arr")) {
    return "Recurring Revenue";
  }

  if (haystack.includes("revenue") || haystack.includes("ebitda") || haystack.includes("gross")) {
    return "Income Statement";
  }

  return "Operating Table";
}

function buildSourceFile(input: IntakeUploadInput, overrides: Partial<SourceFile> = {}): SourceFile {
  return {
    id: overrides.id ?? createId("file"),
    name: input.name,
    fileType: input.fileType || getFileType(input.name),
    uploadDate: input.uploadDate ?? new Date().toISOString(),
    detectedCategory: input.detectedCategory,
    status: overrides.status ?? input.status ?? "Connected",
    pages: overrides.pages ?? 1,
    owner: overrides.owner ?? "Deal team",
    supportedForParsing: overrides.supportedForParsing ?? isSupportedStructuredFile(input.name),
  };
}

function parseSheetRows(sheetRows: string[][], tableName: string, isCsv: boolean) {
  const headerRowIndex = findHeaderRowIndex(sheetRows);
  const headerRow = headerRowIndex >= 0 ? sheetRows[headerRowIndex] : [];

  const extractedRows = sheetRows
    .map((row, index) => {
      if (row.every((cell) => !cell)) {
        return null;
      }

      if (index === headerRowIndex) {
        return null;
      }

      const labelIndex = row.findIndex((cell) => Boolean(cell.trim()));

      if (labelIndex === -1) {
        return null;
      }

      const label = row[labelIndex]?.trim();

      if (!label || label.length < 2) {
        return null;
      }

      const valueIndex = (() => {
        for (let currentIndex = row.length - 1; currentIndex > labelIndex; currentIndex -= 1) {
          if (isNumericLike(row[currentIndex] ?? "")) {
            return currentIndex;
          }
        }

        return -1;
      })();

      if (valueIndex === -1) {
        return null;
      }

      const value = row[valueIndex]?.trim();
      const location = isCsv
        ? `row ${index + 1}`
        : `${tableName}!${columnLabel(valueIndex + 1)}${index + 1}`;
      const periodSource = headerRow[valueIndex] || headerRow.find((cell) => detectPeriod(cell) !== "Current Period") || tableName;

      return {
        row: {
          label,
          value,
          location,
          rawCells: row,
        } satisfies ExtractedDataRow,
        period: detectPeriod(periodSource),
      };
    })
    .filter((item): item is { row: ExtractedDataRow; period: string } => Boolean(item));

  const issues: string[] = [];

  if (headerRowIndex === -1) {
    issues.push("Missing headers");
  }

  if (extractedRows.some((item) => item.row.value.includes("%")) && extractedRows.some((item) => item.row.value.includes("$"))) {
    issues.push("Unit ambiguity");
  }

  const duplicateLabels = new Set<string>();
  const seenLabels = new Set<string>();
  for (const item of extractedRows) {
    const normalized = normalizeLabel(item.row.label);
    if (seenLabels.has(normalized)) {
      duplicateLabels.add(normalized);
    }
    seenLabels.add(normalized);
  }
  if (duplicateLabels.size > 0) {
    issues.push("Duplicate labels");
  }

  if (extractedRows.length === 0) {
    issues.push("No structured rows detected");
  }

  const confidence = Math.max(36, Math.min(97, 88 - issues.length * 11 + extractedRows.length));
  const period =
    extractedRows[0]?.period ??
    detectPeriod(`${tableName} ${headerRow.join(" ")}`);

  return {
    rows: extractedRows,
    issues: issues.length > 0 ? issues : ["None"],
    confidence,
    period,
  };
}

function classifyMapping(label: string, tableName: string): MappingClassification {
  const normalizedLabel = normalizeLabel(label);
  const normalizedContext = normalizeLabel(`${tableName} ${label}`);
  const normalizedTableName = normalizeLabel(tableName);

  for (const rule of categoryHeuristics) {
    const exactMatches =
      rule.exact
        ?.map((keyword) => normalizeLabel(keyword))
        .filter(
          (keyword) =>
            keyword &&
            (normalizedLabel === keyword ||
              normalizedLabel.endsWith(keyword) ||
              normalizedLabel.startsWith(keyword)),
        ) ?? [];

    if (exactMatches.length > 0) {
      return {
        mappedCategory: rule.category,
        confidence: 96,
        status: "Approved" as MappingStatus,
        reasoning: `Exact label heuristic matched "${label}" into ${rule.category}.`,
        matchStrength: "exact" as const,
      };
    }

    const normalizedKeywords = rule.keywords.map((keyword) => normalizeLabel(keyword));
    const labelMatches = normalizedKeywords.filter(
      (keyword) => keyword && normalizedLabel.includes(keyword),
    );
    const contextMatches = normalizedKeywords.filter(
      (keyword) =>
        keyword &&
        !labelMatches.includes(keyword) &&
        normalizedContext.includes(keyword),
    );

    if (labelMatches.length > 0 || contextMatches.length > 0) {
      const tableBonus =
        normalizedTableName.includes("p l") ||
        normalizedTableName.includes("income statement") ||
        normalizedTableName.includes("qoe")
          ? 8
          : 0;
      const confidence =
        labelMatches.length > 0
          ? Math.min(94, 84 + labelMatches.length * 6 + tableBonus)
          : Math.min(82, 70 + contextMatches.length * 4 + tableBonus);
      const status =
        labelMatches.length > 0 && confidence >= 88
          ? ("Approved" as MappingStatus)
          : ("Pending" as MappingStatus);
      const matchedKeywords =
        labelMatches.length > 0 ? labelMatches : contextMatches;

      return {
        mappedCategory: rule.category,
        confidence,
        status,
        reasoning:
          labelMatches.length > 0
            ? `Mapped using direct label heuristic: ${matchedKeywords.join(", ")}.`
            : `Mapped using contextual sheet heuristic from ${tableName}: ${matchedKeywords.join(", ")}.`,
        matchStrength: labelMatches.length > 0 ? ("label" as const) : ("context" as const),
      };
    }
  }

  return {
    mappedCategory: unmappedCategory,
    confidence: 32,
    status: "Needs Review" as MappingStatus,
    reasoning: "No deterministic keyword match found in the current heuristic set.",
    matchStrength: "none" as const,
  };
}

function buildMappingRowsForExtractedItem(
  sourceFileId: string,
  tableName: string,
  extractedRows: Array<{ row: ExtractedDataRow; period: string }>,
): {
  mappingRows: MappingRow[];
  duplicateSummary: {
    collapsedDuplicates: number;
    conflictingDuplicateGroups: number;
  };
} {
  const initialRows = extractedRows.map(({ row, period }) =>
    buildRoutingAwareMappingRow({
      sourceFileId,
      period,
      row,
      tableName,
    }),
  );
  const mappingRows = collapseDuplicateCandidates(initialRows);

  return {
    mappingRows,
    duplicateSummary: summarizeDuplicateRouting(mappingRows),
  };
}

async function parseStructuredUpload(input: IntakeUploadInput): Promise<{
  sourceFile: SourceFile;
  extractedItems: ExtractedItem[];
  mappingRows: MappingRow[];
}> {
  if (!input.file) {
    return {
      sourceFile: buildSourceFile(input, {
        status: "Connected",
        supportedForParsing: false,
      }),
      extractedItems: [],
      mappingRows: [],
    };
  }

  const sourceFileId = createId("file");
  const sourceFile = buildSourceFile(input, {
    id: sourceFileId,
    status: "Indexed",
    supportedForParsing: true,
  });
  const isCsv = getFileExtension(input.name) === "csv";
  const workbook = XLSX.read(await input.file.arrayBuffer(), {
    type: "array",
    dense: true,
  });

  const extractedItems: ExtractedItem[] = [];
  const mappingRows: MappingRow[] = [];

  for (const sheetName of workbook.SheetNames) {
    const worksheet = workbook.Sheets[sheetName];
    const rows = (XLSX.utils.sheet_to_json(worksheet, {
      header: 1,
      raw: false,
      defval: "",
      blankrows: false,
    }) as unknown[][]).map((row) =>
      row.map((cell) => String(cell ?? "").trim()),
    );

    const parsedRows = parseSheetRows(rows, sheetName, isCsv);
    const tableRows = parsedRows.rows.map((entry) => entry.row);
    const itemId = createId("ext");
    const title = isCsv ? stripExtension(input.name) : sheetName;
    const { mappingRows: tableMappingRows, duplicateSummary } = buildMappingRowsForExtractedItem(
      sourceFileId,
      title,
      parsedRows.rows,
    );
    const routingSummary = summarizeExtractedItemRouting(tableMappingRows);
    const detectedTableType = detectTableType(title, tableRows);
    const issueFlags = parsedRows.issues.filter((issue) => {
      if (issue !== "Duplicate labels") {
        return true;
      }

      return duplicateSummary.conflictingDuplicateGroups > 0;
    });
    const summary =
      tableRows.length > 0
        ? `${tableRows.length} candidate rows detected from ${title}. ${tableRows
            .slice(0, 3)
            .map((row) => row.label)
            .join(", ")}.`
        : `No structured financial rows were detected from ${title}.`;

    extractedItems.push({
      id: itemId,
      title,
      tableName: title,
      sourceFileId,
      period: parsedRows.period,
      confidence: parsedRows.confidence,
      detectedTableType,
      issueFlags: issueFlags.length > 0 ? issueFlags : ["None"],
      summary,
      rows: tableRows.slice(0, 12),
      routingBucket: routingSummary.routingBucket,
      coreRowCount: routingSummary.coreRowCount,
      supportingRowCount: routingSummary.supportingRowCount,
    });

    mappingRows.push(...tableMappingRows);
  }

  return {
    sourceFile: {
      ...sourceFile,
      pages: Math.max(1, workbook.SheetNames.length),
    },
    extractedItems,
    mappingRows,
  };
}

function buildPlaceholderUpload(input: IntakeUploadInput) {
  return {
    sourceFile: buildSourceFile(input, {
      status: input.status ?? "Connected",
      supportedForParsing: false,
      pages: undefined,
    }),
    extractedItems: [] as ExtractedItem[],
    mappingRows: [] as MappingRow[],
  };
}

export async function scanUploadedFiles(uploads: IntakeUploadInput[]) {
  const sourceFiles: SourceFile[] = [];
  const extractedItems: ExtractedItem[] = [];
  const mappingRows: MappingRow[] = [];

  for (const upload of uploads) {
    const parsed = isSupportedStructuredFile(upload.name)
      ? await parseStructuredUpload(upload)
      : buildPlaceholderUpload(upload);

    sourceFiles.push(parsed.sourceFile);
    extractedItems.push(...parsed.extractedItems);
    mappingRows.push(...parsed.mappingRows);
  }

  const possibleIssues =
    extractedItems.filter((item) =>
      item.issueFlags.some((flag) => flag.toLowerCase() !== "none"),
    ).length +
    sourceFiles.filter((file) => !file.supportedForParsing).length;
  const readinessScore = Math.max(
    24,
    Math.min(
      96,
      42 +
        extractedItems.length * 8 +
        mappingRows.filter((row) => row.mappedCategory !== unmappedCategory).length * 4 -
        possibleIssues * 8,
    ),
  );

  return {
    sourceFiles,
    extractedItems,
    mappingRows,
    scanSummary: {
      fileCount: uploads.length,
      financialTables: extractedItems.length,
      possibleIssues,
      readinessScore,
    },
  } satisfies IntakeScanResult;
}

function buildGeneratedException(
  partial: Omit<ExceptionItem, "id" | "status" | "assignedOwner" | "origin"> & {
    issueKey: string;
  },
  previous?: ExceptionItem,
): ExceptionItem {
  return {
    id: previous?.id ?? createId("review"),
    origin: "generated",
    assignedOwner: previous?.assignedOwner ?? "Deal team",
    status: previous?.status ?? "Open",
    scope: previous?.scope ?? partial.scope ?? ("core" as ReviewScope),
    issueLevel: previous?.issueLevel ?? partial.issueLevel ?? ("row" as ReviewIssueLevel),
    issueClass:
      previous?.issueClass ??
      partial.issueClass ??
      ((partial.issueLevel ?? "row") === "table"
        ? ("table_warning" as ReviewIssueClass)
        : (partial.blocksExport ??
            (partial.severity === "High" || partial.severity === "Critical"))
          ? ("real_core_blocker" as ReviewIssueClass)
          : ("non_blocking_mapping_bug" as ReviewIssueClass)),
    blocksExport:
      previous?.blocksExport ??
      partial.blocksExport ??
      (partial.severity === "High" || partial.severity === "Critical"),
    ...partial,
  };
}

function detectRowIssue(params: {
  row: MappingRow;
  definedItem?: DefinedItem;
}) {
  const normalizedLabel = normalizeLabel(params.row.rawLineItemLabel);
  const valueType = getValueTypeForGuardrail(params.row);
  const labelFamily = inferLabelFamily(params.row);
  const isPercentValue = valueType === "percentage";
  const isCurrencyLike = valueType === "currency";

  if (
    labelFamily === "retention_kpi" &&
    ["Revenue", "COGS", "Operating Expenses", "Gross Profit", "EBITDA"].includes(params.row.mappedCategory)
  ) {
    return {
      issueKey: `kpi-scope-${params.row.id}`,
      issueClass: "kpi_scope_issue" as ReviewIssueClass,
      severity: "Low" as Severity,
      blocksExport: false,
      category: "KPI scope issue",
      suggestedResolution: "Keep this row in the KPI lane instead of forcing it into the core P&L taxonomy.",
      detail:
        "The label looks like a retention KPI, so it should not be treated as a core currency revenue line.",
    };
  }

  if (
    params.row.mappedCategory === "Headcount" &&
    (isCurrencyLike ||
      normalizedLabel.includes("capex") ||
      normalizedLabel.includes("capital expend") ||
      normalizedLabel.includes("revenue") ||
      normalizedLabel.includes("expense"))
  ) {
    return {
      issueKey: `mapping-bug-${params.row.id}`,
      issueClass: "non_blocking_mapping_bug" as ReviewIssueClass,
      severity: "Medium" as Severity,
      blocksExport: false,
      category: "Possible mapping bug",
      suggestedResolution: "Re-map this row before using it as a databook input.",
      detail:
        "The row is currently mapped to Headcount, but the label or value looks more like a financial amount than an employee count.",
    };
  }

  if (
    params.row.mappedCategory === "Customer Churn" &&
    !isPercentValue
  ) {
    return {
      issueKey: `mapping-bug-${params.row.id}`,
      issueClass: "non_blocking_mapping_bug" as ReviewIssueClass,
      severity: "Medium" as Severity,
      blocksExport: false,
      category: "Possible mapping bug",
      suggestedResolution: "Check whether this row is really churn or another KPI before approving it.",
      detail:
        "The row is mapped to Customer Churn, but the source value does not look like a percentage.",
    };
  }

  if (
    ["Revenue", "COGS", "Operating Expenses", "CapEx", "ARR"].includes(params.row.mappedCategory) &&
    isPercentValue
  ) {
    return {
      issueKey: `mapping-bug-${params.row.id}`,
      issueClass:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? ("kpi_scope_issue" as ReviewIssueClass)
          : ("non_blocking_mapping_bug" as ReviewIssueClass),
      severity:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? ("Low" as Severity)
          : coreBlockingCategories.has(params.row.mappedCategory)
            ? ("High" as Severity)
            : ("Medium" as Severity),
      blocksExport:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? false
          : coreBlockingCategories.has(params.row.mappedCategory),
      category:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? "KPI scope issue"
          : "Possible mapping bug",
      suggestedResolution:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? "Keep this KPI ratio out of the core currency lines."
          : "Re-map this row before relying on it as a core databook input.",
      detail:
        labelFamily === "retention_kpi" || labelFamily === "churn_kpi"
          ? "The row looks like a KPI ratio, so it should not be accepted as a core currency metric."
          : "The row is mapped to a currency metric, but the source value looks like a percentage.",
    };
  }

  if (
    params.definedItem?.unit === "unknown" &&
    params.row.entersCorePipeline !== false
  ) {
    return {
      issueKey: `unit-review-${params.row.id}`,
      issueClass: "non_blocking_mapping_bug" as ReviewIssueClass,
      severity: "Low" as Severity,
      blocksExport: false,
      category: "Unclear unit",
      suggestedResolution: "Confirm the source unit before relying on the row in the databook.",
      detail:
        "The system could not confidently determine the unit for this core row.",
    };
  }

  return null;
}

function valuesConflict(left: number | null, right: number | null) {
  if (left === null || right === null) {
    return false;
  }

  const scale = Math.max(Math.abs(left), Math.abs(right), 1);

  return Math.abs(left - right) / scale > 0.05;
}

function buildGeneratedExceptions(
  existingItems: ExceptionItem[],
  mappingRows: MappingRow[],
  extractedItems: ExtractedItem[],
  definedItems: DefinedItem[],
  formulaInputs: ReturnType<typeof buildFormulaInputAssignments>,
  metrics: DatabookMetricRecord[],
) {
  const previousGenerated = new Map(
    existingItems
      .filter((item) => item.origin === "generated" && item.issueKey)
      .map((item) => [item.issueKey as string, item]),
  );
  const manualItems = existingItems.filter((item) => item.origin !== "generated");
  const generatedItems: ExceptionItem[] = [];
  const coreMappingRows = mappingRows.filter((row) => row.entersCorePipeline !== false);
  const assignedFormulaInputs = formulaInputs.filter((input) => input.assignedValue !== null);
  const definedItemIndex = new Map(definedItems.map((item) => [item.id, item]));

  for (const row of coreMappingRows) {
    const definedItem = definedItemIndex.get(`defined-${row.id}`);
    const detectedRowIssue = detectRowIssue({
      row,
      definedItem,
    });

    if (detectedRowIssue) {
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey: detectedRowIssue.issueKey,
            mappingRowId: row.id,
            sourceFileId: row.sourceFileId,
            scope: "core",
            issueLevel: "row",
            issueClass: detectedRowIssue.issueClass,
            blocksExport: detectedRowIssue.blocksExport,
            severity: detectedRowIssue.severity,
            category: detectedRowIssue.category,
            affectedLineItem: `${row.rawLineItemLabel} -> ${row.mappedCategory}`,
            suggestedResolution: detectedRowIssue.suggestedResolution,
            detail: detectedRowIssue.detail,
          },
          previousGenerated.get(detectedRowIssue.issueKey),
        ),
      );

      continue;
    }

    if (row.duplicateRole === "primary" && row.duplicateConflict) {
      const issueKey = `duplicate-${row.duplicateGroupKey ?? row.id}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            mappingRowId: row.id,
            sourceFileId: row.sourceFileId,
            scope: "core",
            issueLevel: "row",
            issueClass: "non_blocking_mapping_bug",
            blocksExport: false,
            severity: "Low",
            category: "Duplicate candidate values",
            affectedLineItem: `${row.rawLineItemLabel} -> ${row.mappedCategory}`,
            suggestedResolution:
              "Confirm which duplicate candidate should remain primary if it needs to feed the databook.",
            detail:
              "Multiple near-duplicate rows were collapsed before Gemini so only one primary row stayed in the core path, but their values or categories were not close enough to merge silently.",
          },
          previousGenerated.get(issueKey),
        ),
      );

      continue;
    }

    if (row.mappedCategory === unmappedCategory) {
      const blocksExport = isCoreBlockingMappingRow(row);
      const issueKey = `unmapped-${row.id}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            mappingRowId: row.id,
            sourceFileId: row.sourceFileId,
            scope: "core",
            issueLevel: "row",
            issueClass: blocksExport ? "real_core_blocker" : "non_blocking_mapping_bug",
            blocksExport,
            severity: blocksExport ? "High" : "Low",
            category: "Unmapped row",
            affectedLineItem: row.rawLineItemLabel,
            suggestedResolution:
              blocksExport
                ? "Assign the row into the core taxonomy or provide the missing upstream P&L input."
                : "Only pull this row into the databook if it should feed a core metric.",
            detail: blocksExport
              ? "A core databook row could not be mapped, so the final workbook may miss a required input."
              : "The row stayed unmapped, but it does not currently threaten the core databook output.",
          },
          previousGenerated.get(issueKey),
        ),
      );
    } else if (row.confidence < 78 || row.status === "Needs Review" || row.status === "Pending") {
      const coreRisk = coreBlockingCategories.has(row.mappedCategory);
      const severity: Severity =
        coreRisk && row.confidence < 50 ? "Medium" : row.confidence < 50 ? "Low" : "Low";
      const issueKey = `mapping-${row.id}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            mappingRowId: row.id,
            sourceFileId: row.sourceFileId,
            scope: "core",
            issueLevel: "row",
            issueClass: "non_blocking_mapping_bug",
            blocksExport: false,
            severity,
            category: row.confidence < 60 ? "Low confidence mapping" : "Pending review",
            affectedLineItem: `${row.rawLineItemLabel} -> ${row.mappedCategory}`,
            suggestedResolution:
              row.mappedCategory === unmappedCategory
                ? "Choose the closest standard category in Mapping Studio."
                : "Review the row and approve or re-map it if it should influence the core databook.",
            detail: `${row.rawLineItemLabel} was mapped with ${row.confidence}% confidence and is still marked as ${row.status}, but it is not treated as a direct export blocker on its own.`,
          },
          previousGenerated.get(issueKey),
        ),
      );
    }
  }

  const conflictSensitiveMetrics = new Set([
    "arr",
    "net_revenue_retention",
    "customer_churn",
    "headcount",
    "capex",
  ]);
  const candidateGroups = new Map<string, typeof assignedFormulaInputs>();
  for (const input of formulaInputs.filter(
    (entry) =>
      conflictSensitiveMetrics.has(entry.outputLineKey) &&
      entry.normalizedValue !== null,
  )) {
    candidateGroups.set(input.outputLineKey, [...(candidateGroups.get(input.outputLineKey) ?? []), input]);
  }
  for (const [outputLineKey, inputs] of candidateGroups.entries()) {
    const uniqueLocations = new Set(inputs.map((input) => `${input.sourceFileName}:${input.sourceLocation}`));
    if (uniqueLocations.size < 2) {
      continue;
    }
    const conflictingValues = inputs.some((input, index) =>
      inputs.slice(index + 1).some((other) => valuesConflict(input.normalizedValue, other.normalizedValue)),
    );

    if (conflictingValues) {
      const issueKey = `conflict-${outputLineKey}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            mappingRowId: definedItems.find((item) => item.outputLineKey === outputLineKey)?.id.replace("defined-", ""),
            sourceFileId: inputs[0]?.sourceFileId,
            scope: "core",
            issueLevel: "row",
            issueClass: "non_blocking_mapping_bug",
            blocksExport: false,
            severity: "Low",
            category: "Conflicting candidate values",
            affectedLineItem: inputs.map((input) => input.rawLabel).join(" / "),
            suggestedResolution: "Confirm which source should drive this metric before relying on the final export.",
            detail: `${inputs.length} candidate rows point to ${outputLineKey.replaceAll("_", " ")}, and their values do not agree closely enough to merge silently.`,
          },
          previousGenerated.get(issueKey),
        ),
      );
    }
  }

  for (const item of extractedItems.filter((entry) => (entry.coreRowCount ?? 0) > 0)) {
    for (const flag of item.issueFlags.filter((value) => value.toLowerCase() !== "none")) {
      const issueKey = `extract-${item.id}-${flag.toLowerCase().replaceAll(/\s+/g, "-")}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            extractedItemId: item.id,
            sourceFileId: item.sourceFileId,
            scope: "core",
            issueLevel: "table",
            issueClass: "table_warning",
            blocksExport: false,
            severity: flag === "Missing headers" ? "Medium" : "Low",
            category: flag,
            affectedLineItem: item.title,
            suggestedResolution: "Validate the staged table before relying on it downstream.",
            detail: `${item.title} still carries the extraction flag: ${flag}, but the system only treats it as blocking if it breaks downstream core metric coverage.`,
          },
          previousGenerated.get(issueKey),
        ),
      );
    }
  }

  for (const input of assignedFormulaInputs.filter((entry) => entry.traceabilityStatus !== "Traced")) {
    const item = definedItems.find((definedItem) => definedItem.id === input.definedItemId);
    if (!item) {
      continue;
    }
    const issueKey = `trace-${item.id}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            sourceFileId: item.sourceFileId,
            scope: "core",
            issueLevel: "row",
          issueClass: "real_core_blocker",
          blocksExport: true,
          severity: item.traceabilityStatus === "Missing" ? "High" : "Medium",
          category: "Traceability gap",
          affectedLineItem: `${item.rawLabel} -> ${item.mappedCategory}`,
          suggestedResolution: "Confirm the source tab and row locator before relying on the databook output.",
          detail:
            item.traceabilityStatus === "Missing"
              ? "The line item is not carrying a stable source reference into the databook."
              : "The line item is only partially traced and should be checked before export.",
        },
        previousGenerated.get(issueKey),
      ),
    );
  }

  for (const metric of metrics.filter(
    (item) =>
      item.status === "Unavailable" &&
      ["Revenue", "COGS", "Operating Expenses"].includes(item.label),
  )) {
    const issueKey = `core-input-${metric.key}`;
      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            scope: "core",
            issueLevel: "metric",
          issueClass: "real_core_blocker",
          blocksExport: true,
          severity: "High",
          category: "Missing core metric input",
          affectedLineItem: metric.label,
          suggestedResolution: `Add or approve the missing mapped inputs so ${metric.label} is present before export.`,
          detail: `${metric.label} is missing from the currently approved mapped rows, so the core P&L cannot be treated as workbook-ready.`,
        },
        previousGenerated.get(issueKey),
      ),
    );
  }

  for (const metric of metrics.filter(
    (item) =>
      ["Gross Profit", "Gross Margin", "EBITDA", "EBITDA Margin"].includes(item.label) &&
      item.status !== "Calculated",
  )) {
    const issueKey = `formula-${metric.key}`;
    const highSeverity =
      metric.label === "Gross Profit" || metric.label === "EBITDA";

      generatedItems.push(
        buildGeneratedException(
          {
            issueKey,
            scope: "core",
            issueLevel: "metric",
          issueClass: "real_core_blocker",
          blocksExport: true,
          severity: highSeverity ? "High" : "Medium",
          category: "Formula not completed",
          affectedLineItem: metric.label,
          suggestedResolution: highSeverity
            ? `Complete the required Revenue, COGS, and Operating Expenses inputs so ${metric.label} is formula-calculated, not just partially staged.`
            : `Complete the upstream core P&L lines so ${metric.label} can be calculated deterministically.`,
          detail:
            metric.status === "Provided"
              ? `${metric.label} has a reported source value, but the databook formula has not been completed from standardized inputs yet.`
              : `${metric.label} is not available because the required upstream inputs have not been completed.`,
        },
        previousGenerated.get(issueKey),
      ),
    );
  }

  return [...manualItems, ...generatedItems];
}

function buildTablePreviewRows(metrics: DatabookMetricRecord[]): OutputPreviewTableRow[] {
  return metrics
    .filter((metric) => metric.status !== "Unavailable")
    .slice(0, 12)
    .map((metric) => ({
      item: metric.label,
      valueA: metric.formattedValue,
      valueB: `${metric.directOrDerived} · ${metric.status}`,
      trace: metric.sourceSummary,
    }));
}

function buildNotesSections(
  deal: Deal,
  usableItems: DefinedItem[],
  openItems: ExceptionItem[],
  metrics: DatabookMetricRecord[],
): OutputPreviewSection[] {
  const openReviewItems = openItems.filter((item) => isBlockingCoreIssue(item) || isNonBlockingRowIssue(item));
  const openTableWarnings = openItems.filter((item) => isTableWarning(item));
  const blockingReviewItems = openItems.filter((item) => isBlockingCoreIssue(item));

  return [
    {
      heading: "Coverage Summary",
      bullets: [
        `${usableItems.length} defined items are currently usable for databook export.`,
        `${deal.sourceFiles.length} source files are connected in the local workspace.`,
        `${metrics.filter((metric) => metric.status !== "Unavailable").length} databook metrics are currently available.`,
      ],
    },
    {
      heading: "Review Position",
      bullets: [
        `${openReviewItems.length} review item${openReviewItems.length === 1 ? "" : "s"} remain open.`,
        `${blockingReviewItems.length} blocking item${blockingReviewItems.length === 1 ? "" : "s"} currently sit in the export path.`,
        `${openTableWarnings.length} table warning${openTableWarnings.length === 1 ? "" : "s"} remain separate from the core export gate.`,
        `${usableItems.filter((item) => item.traceabilityStatus === "Traced").length} items carry full source traceability.`,
      ],
    },
  ];
}

function buildOutputAssets(
  deal: Deal,
  previousOutputs: OutputAsset[] = [],
  options?: { regeneratedOutputId?: string },
) {
  const usableItems = (deal.definedItems ?? []).filter(
    (item) =>
      item.reviewStatus !== "Flagged" &&
      item.mappedCategory !== unmappedCategory &&
      item.normalizedValue !== null,
  );
  const metrics = getDatabookMetrics(deal);
  const openItems = deal.exceptions.filter((item) => item.status === "Open");
  const blockingItems = openItems.filter((item) => isBlockingCoreIssue(item));
  const availableMetrics = metrics.filter((metric) => metric.status !== "Unavailable").length;
  const databookReadiness = getDatabookReadiness(metrics);
  const completeness = metrics.length
    ? Math.round((availableMetrics / metrics.length) * 100)
    : 0;
  const ready =
    databookReadiness.ready &&
    blockingItems.length === 0 &&
    usableItems.length > 0 &&
    availableMetrics > 0;

  const makeOutput = (
    id: string,
    name: string,
    previewType: OutputAsset["previewType"],
    reviewStatus: string,
  ): OutputAsset => {
    const previous = previousOutputs.find((output) => output.id === id);
    const status: OutputStatus =
      ready
        ? "Ready"
        : openItems.length > 0 || !databookReadiness.ready
          ? "Needs Review"
          : usableItems.length > 0
            ? "In Progress"
            : "Queued";
    const generatedDate =
      options?.regeneratedOutputId === id || !previous?.generatedDate
        ? new Date().toISOString()
        : previous.generatedDate;

    return {
      id,
      name,
      status,
      generatedDate,
      completeness,
      sourceLinked: usableItems.every((item) => item.traceabilityStatus === "Traced"),
      reviewStatus,
      rowCount: availableMetrics,
      previewType,
      previewRows:
        previewType === "table"
          ? buildTablePreviewRows(metrics)
          : undefined,
      previewSections:
        previewType === "sections"
          ? buildNotesSections(deal, usableItems, openItems, metrics)
          : undefined,
    };
  };

  return [
    makeOutput(
      "databook-preview",
      "Databook Preview",
      "table",
      ready ? "Ready for export from current approved mappings" : "Waiting for mapping and review sign-off",
    ),
    makeOutput(
      "pnl-workbook",
      "P&L Workbook",
      "table",
      ready ? "Workbook rows reflect approved mappings" : "Workbook remains provisional until review clears",
    ),
    makeOutput(
      "ic-prep-notes",
      "IC Prep Notes",
      "sections",
      openItems.length === 0 ? "Notes aligned to the current clean mapping set" : "Notes still include unresolved workflow caveats",
    ),
    makeOutput(
      "review-notes-package",
      "Review Notes Package",
      "sections",
      `${openItems.length} review items currently represented in the package`,
    ),
    makeOutput(
      "crm-update-package",
      "CRM Update Package",
      "sections",
      "Still a mock downstream package, but now grounded in the local mapping state",
    ),
  ];
}

function buildQualityPanel(sourceFiles: SourceFile[], extractedItems: ExtractedItem[]) {
  const routedItems = extractedItems.filter((item) => (item.coreRowCount ?? 0) > 0);
  const issueFlags = routedItems.flatMap((item) => item.issueFlags);
  const duplicateFiles = sourceFiles.length - new Set(sourceFiles.map((file) => file.name.toLowerCase())).size;
  const confidenceSummary = routedItems.reduce(
    (summary, item) => {
      if (item.confidence >= 85) {
        summary.high += 1;
      } else if (item.confidence >= 65) {
        summary.medium += 1;
      } else {
        summary.low += 1;
      }

      return summary;
    },
    { high: 0, medium: 0, low: 0 },
  );

  return {
    missingHeaders: issueFlags.filter((flag) => flag === "Missing headers").length,
    duplicateFiles: Math.max(0, duplicateFiles),
    unreadablePages: issueFlags.filter((flag) => flag === "No structured rows detected").length,
    unitAmbiguity: issueFlags.filter((flag) => flag === "Unit ambiguity").length,
    confidenceSummary,
  };
}

function stageLabelFromWorkflow(statusKey: DealStatus) {
  return statusKey;
}

function mapWorkflowStageToStatus(deal: Deal): DealStatus {
  const workflow = getWorkflowSnapshot(deal);

  switch (workflow.currentActionStage) {
    case "intake":
      return "Intake";
    case "extraction":
      return "Extraction";
    case "mapping":
      return "Mapping";
    case "review":
      return "Review";
    case "outputs":
      return workflow.readyOutputs > 0 ? "Output Ready" : "Review";
    default:
      return "Initial Scan";
  }
}

export function refreshDealState(deal: Deal, options?: { regeneratedOutputId?: string }) {
  const sourceFiles = deal.sourceFiles.map((file) => ({
    ...file,
    supportedForParsing:
      file.supportedForParsing ?? isSupportedStructuredFile(file.name),
  }));
  const mappingRows = applyDeterministicMappingGuardrails(
    deal.mappingRows.map((row) =>
      ensureMappingRowRouting(row, deal.extractedItems),
    ),
  );
  const definedItems = buildDefinedItems({
    sourceFiles,
    extractedItems: deal.extractedItems,
    mappingRows,
  });
  const formulaInputs = buildFormulaInputAssignments(definedItems);
  const metrics = buildDatabookMetricsFromFormulaInputs(formulaInputs);
  const databookReadiness = getDatabookReadiness(metrics);
  const traceabilityRecords = buildTraceabilityRecords(metrics, formulaInputs);
  const exceptions = buildGeneratedExceptions(
    deal.exceptions,
    mappingRows,
    deal.extractedItems,
    definedItems,
    formulaInputs,
    metrics,
  );
  const qualityPanel = buildQualityPanel(sourceFiles, deal.extractedItems);
  const intermediateDeal = {
    ...deal,
    sourceFiles,
    definedItems,
    formulaInputs,
    databookMetrics: metrics,
    traceabilityRecords,
    sourceFilesConnected: sourceFiles.length > 0,
    sourceFileIds: sourceFiles.map((file) => file.id),
    exceptions,
    qualityPanel,
    mappingRows,
  };
  const outputs = buildOutputAssets(intermediateDeal, deal.outputs, options);
  const structuredFiles = sourceFiles.filter((file) => file.supportedForParsing).length;
  const extractedFileCount = new Set(deal.extractedItems.map((item) => item.sourceFileId)).size;
  const openItems = exceptions.filter((item) => item.status === "Open");
  const blockingOpenItems = openItems.filter((item) => isBlockingCoreIssue(item));
  const nonBlockingOpenItems = openItems.filter((item) => isNonBlockingRowIssue(item));
  const openTableWarnings = openItems.filter((item) => isTableWarning(item));
  const approvedRows = mappingRows.filter(
    (row) => row.status === "Approved" || row.status === "Rule Applied",
  ).length;
  const coreDefinedItems = definedItems.filter((item) => item.entersCorePipeline !== false);
  const mappedDefinedItems = coreDefinedItems.filter(
    (item) => item.mappedCategory !== unmappedCategory,
  ).length;
  const tracedDefinedItems = coreDefinedItems.filter(
    (item) => item.traceabilityStatus === "Traced",
  ).length;
  const availableMetrics = metrics.filter((metric) => metric.status !== "Unavailable").length;
  const extractionProgress =
    structuredFiles > 0
      ? Math.max(20, Math.min(100, Math.round((extractedFileCount / structuredFiles) * 100)))
      : sourceFiles.length > 0
        ? 68
        : 0;
  const baseReadinessScore = Math.max(
    18,
    Math.min(
      96,
      28 +
        deal.extractedItems.length * 6 +
        approvedRows * 3 +
        mappedDefinedItems * 2 +
        tracedDefinedItems * 2 +
        availableMetrics * 3 +
        databookReadiness.coreDirectAvailableCount * 5 +
        databookReadiness.requiredCalculatedMetricReadyCount * 4 -
        Math.min(2, openTableWarnings.length) -
        nonBlockingOpenItems.length -
        blockingOpenItems.length * 10,
    ),
  );
  const readinessCeiling =
    databookReadiness.ready
      ? 96
      : databookReadiness.missingCoreMetricKeys.length > 0
        ? 56
        : databookReadiness.incompleteFormulaMetricKeys.length > 0
          ? 74
          : 88;
  const readinessScore = Math.min(baseReadinessScore, readinessCeiling);

  const refreshed = {
    ...intermediateDeal,
    outputs,
    outputStatus: outputs.find((output) => output.id === "databook-preview")?.status ?? "Queued",
    outputsReady:
      databookReadiness.ready &&
      outputs.some((output) => output.status === "Ready"),
    exceptionCount: blockingOpenItems.length,
    extractionProgress,
    readinessScore,
  };
  const workflow = getWorkflowSnapshot(refreshed);
  const completedStages = workflow.stages.filter(
    (stage) => stage.status === "Complete" || stage.status === "Ready",
  ).length;
  const inProgressStages = workflow.stages.filter((stage) => stage.status === "In Progress").length;

  return {
    ...refreshed,
    status: mapWorkflowStageToStatus(refreshed),
    stage: stageLabelFromWorkflow(mapWorkflowStageToStatus(refreshed)),
    workflowProgress: Math.min(100, completedStages * 20 + inProgressStages * 10),
    ttmRevenue:
      metrics.find((metric) => metric.key === "revenue")?.value ?? deal.ttmRevenue,
    ttmEbitda:
      metrics.find((metric) => metric.key === "ebitda")?.value ?? deal.ttmEbitda,
  };
}

export function buildSeedDeals(seedDeals: Deal[]) {
  return seedDeals.map((deal) =>
    refreshDealState({
      ...deal,
      sourceFileIds: deal.sourceFiles.map((file) => file.id),
      outputStatus: deal.outputs.find((output) => output.id === "databook-preview")?.status ?? "Queued",
      exceptions: deal.exceptions.map((item) => ({
        ...item,
        origin: item.origin ?? "manual",
      })),
      outputs: deal.outputs.map((output) => ({
        ...output,
        rowCount:
          output.rowCount ??
          output.previewRows?.length ??
          output.previewSections?.reduce((total, section) => total + section.bullets.length, 0) ??
          0,
      })),
      sourceFiles: deal.sourceFiles.map((file) => ({
        ...file,
        supportedForParsing: file.supportedForParsing ?? isSupportedStructuredFile(file.name),
      })),
    }),
  );
}

export function createNewDealFromScan(params: {
  dealName: string;
  sector: string;
  scanResult: IntakeScanResult;
}): Deal {
  const now = new Date().toISOString();
  const baseDeal: Deal = {
    id:
      `${slugify(params.dealName || "deal")}-${Date.now().toString(36).slice(-5)}` ||
      createId("deal"),
    targetCompanyName: params.dealName || "New Deal",
    sector: params.sector || "General",
    status: "Initial Scan",
    sourceFileIds: params.scanResult.sourceFiles.map((file) => file.id),
    outputStatus: "Queued",
    sourceFilesConnected: params.scanResult.sourceFiles.length > 0,
    extractionProgress: 0,
    exceptionCount: 0,
    outputsReady: false,
    seller: "Local pilot",
    sponsor: "Undisclosed",
    geography: "Not yet specified",
    stage: "Initial Scan",
    enterpriseValue: 0,
    ttmRevenue: 0,
    ttmEbitda: 0,
    readinessScore: params.scanResult.scanSummary.readinessScore,
    workflowProgress: 8,
    recentActivity: [
      {
        id: createId("activity"),
        title: "Deal created from local intake",
        description: `${params.scanResult.sourceFiles.length} files were scanned locally and staged into extraction.`,
        timestamp: now,
      },
    ],
    sourceFiles: params.scanResult.sourceFiles,
    extractedItems: params.scanResult.extractedItems,
    mappingRows: params.scanResult.mappingRows,
    exceptions: [],
    outputs: [],
    copilotPrompts: [
      {
        id: "prompt-1",
        label: "Where did EBITDA come from?",
        response: "The assistant can now trace mapped values back to the uploaded spreadsheet rows.",
      },
      {
        id: "prompt-2",
        label: "Summarize review blockers",
        response: "The review queue now reflects the generated mapping and extraction issues from local files.",
      },
    ],
    qualityPanel: {
      missingHeaders: 0,
      duplicateFiles: 0,
      unreadablePages: 0,
      unitAmbiguity: 0,
      confidenceSummary: { high: 0, medium: 0, low: 0 },
    },
  };

  return refreshDealState(baseDeal);
}

export function mergeScanResultIntoDeal(deal: Deal, scanResult: IntakeScanResult) {
  const nextDeal: Deal = {
    ...deal,
    sourceFiles: [...deal.sourceFiles, ...scanResult.sourceFiles],
    extractedItems: [...deal.extractedItems, ...scanResult.extractedItems],
    mappingRows: [...deal.mappingRows, ...scanResult.mappingRows],
    recentActivity: [
      {
        id: createId("activity"),
        title: "Local intake scan completed",
        description: `${scanResult.sourceFiles.length} new files were parsed locally into extraction and mapping state.`,
        timestamp: new Date().toISOString(),
      },
      ...deal.recentActivity,
    ].slice(0, 8),
  };

  return refreshDealState(nextDeal);
}

export function applyMappingRowUpdate(
  deal: Deal,
  rowId: string,
  updater: (row: MappingRow) => MappingRow,
) {
  return refreshDealState({
    ...deal,
    mappingRows: deal.mappingRows.map((row) => {
      if (row.id !== rowId) {
        return row;
      }

      const nextRow = updater(row);

      if (nextRow.mappedCategory !== row.mappedCategory) {
        return {
          ...nextRow,
          definitionHint: undefined,
          directOrDerivedHint: undefined,
          dependencyCandidatesHint: undefined,
          interpretationProvider:
            nextRow.interpretationProvider === "gemini" ? undefined : nextRow.interpretationProvider,
        };
      }

      return nextRow;
    }),
  });
}

export function applyReviewItemUpdate(
  deal: Deal,
  reviewItemId: string,
  status: ExceptionItem["status"],
) {
  const nextExceptions = deal.exceptions.map((item) =>
    item.id === reviewItemId ? { ...item, status } : item,
  );
  const linkedItem = nextExceptions.find((item) => item.id === reviewItemId);
  const nextRows = linkedItem?.mappingRowId
    ? deal.mappingRows.map((row) =>
        row.id === linkedItem.mappingRowId
          ? {
              ...row,
              status:
                status === "Deferred"
                  ? "Needs Review"
                  : row.mappedCategory === unmappedCategory
                    ? row.status
                    : ("Approved" as MappingStatus),
            }
          : row,
      )
    : deal.mappingRows;

  return refreshDealState({
    ...deal,
    exceptions: nextExceptions,
    mappingRows: nextRows,
  });
}

export function regenerateDealOutput(deal: Deal, outputId: string) {
  return refreshDealState(deal, { regeneratedOutputId: outputId });
}

export function buildDatabookExportRows(deal: Deal) {
  const traceabilityRecords = deal.traceabilityRecords ?? [];

  return getDatabookMetrics(deal)
    .filter((metric) => metric.status !== "Unavailable")
    .map((metric) => {
      const linkedRecords = traceabilityRecords.filter(
        (record) => record.outputMetricKey === metric.key,
      );

      return {
        source_file:
          dedupeStrings(linkedRecords.map((record) => record.sourceFileName)).join(" | ") ||
          (metric.status === "Calculated"
            ? "Calculated from source-backed inputs"
            : "Unknown file"),
        source_location:
          dedupeStrings(linkedRecords.map((record) => record.sourceLocation)).join(" | ") ||
          metric.sourceSummary,
        raw_label: metric.label,
        raw_value: metric.formattedValue,
        mapped_category: metric.label,
        direct_or_derived: metric.directOrDerived,
        confidence:
          metric.directOrDerived === "Derived" ? "formula-backed" : "source-backed",
        status: metric.status,
        reasoning:
          metric.directOrDerived === "Derived"
            ? `Calculated deterministically using ${metric.formula}.`
            : metric.definition,
        definition: metric.definition,
        traceability_status: metric.traceabilityStatus,
      };
    });
}
