import { dedupeStrings, formatMetricValue, getPrimaryPeriod } from "@/lib/dataroom-utils";
import { DatabookMetricRecord, FormulaInputAssignment } from "@/lib/types";

const coreDirectMetricKeys = ["revenue", "cogs", "operating-expenses"] as const;
const requiredCalculatedMetricKeys = [
  "gross-profit",
  "gross-margin",
  "ebitda",
  "ebitda-margin",
] as const;

export interface DatabookReadinessSummary {
  ready: boolean;
  coreDirectMetricCount: number;
  coreDirectAvailableCount: number;
  requiredCalculatedMetricCount: number;
  requiredCalculatedMetricReadyCount: number;
  missingCoreMetricKeys: string[];
  incompleteFormulaMetricKeys: string[];
}

function buildMetric(params: {
  key: string;
  outputLineKey: string;
  label: string;
  period: string;
  value: number | null;
  status: DatabookMetricRecord["status"];
  formula: string;
  definition: string;
  rationale: string;
  calculationType: DatabookMetricRecord["calculationType"];
  directOrDerived: DatabookMetricRecord["directOrDerived"];
  formulaDependencies: string[];
  sourceInputIds: string[];
  sourceSummary: string;
  traceabilityStatus: DatabookMetricRecord["traceabilityStatus"];
  format: DatabookMetricRecord["format"];
}) {
  return {
    key: params.key,
    outputLineKey: params.outputLineKey,
    label: params.label,
    period: params.period,
    value: params.value,
    formattedValue: formatMetricValue(params.value, params.format),
    status: params.status,
    formula: params.formula,
    definition: params.definition,
    rationale: params.rationale,
    calculationType: params.calculationType,
    directOrDerived: params.directOrDerived,
    formulaDependencies: params.formulaDependencies,
    sourceInputIds: params.sourceInputIds,
    sourceSummary: params.sourceSummary,
    traceabilityStatus: params.traceabilityStatus,
    format: params.format,
  } satisfies DatabookMetricRecord;
}

function getApprovedInputsByLineKey(inputs: FormulaInputAssignment[], outputLineKey: string) {
  return inputs.filter(
    (input) =>
      input.outputLineKey === outputLineKey &&
      input.assignedValue !== null,
  );
}

function sumInputs(inputs: FormulaInputAssignment[]) {
  if (inputs.length === 0) {
    return null;
  }

  return inputs.reduce((total, input) => total + (input.assignedValue ?? 0), 0);
}

function firstInputValue(inputs: FormulaInputAssignment[]) {
  return inputs[0]?.assignedValue ?? null;
}

function getTraceabilityStatus(inputs: FormulaInputAssignment[]) {
  if (inputs.length === 0) {
    return "Missing" as const;
  }

  if (inputs.every((input) => input.traceabilityStatus === "Traced")) {
    return "Traced" as const;
  }

  if (inputs.some((input) => input.traceabilityStatus !== "Missing")) {
    return "Partial" as const;
  }

  return "Missing" as const;
}

function buildSourceSummary(
  inputs: FormulaInputAssignment[],
  fallback: string,
  options?: { derived?: boolean; formula?: string },
) {
  if (inputs.length === 0) {
    return fallback;
  }

  const sourceSummary = dedupeStrings(
    inputs.map((input) => `${input.sourceFileName} · ${input.sourceLocation}`),
  ).join(" | ");

  if (options?.derived && options.formula) {
    return `${options.formula} | Inputs: ${sourceSummary}`;
  }

  return sourceSummary;
}

export function getDatabookReadiness(metrics: DatabookMetricRecord[]): DatabookReadinessSummary {
  const metricIndex = new Map(metrics.map((metric) => [metric.key, metric]));
  const missingCoreMetricKeys = coreDirectMetricKeys.filter(
    (key) => metricIndex.get(key)?.status === "Unavailable" || !metricIndex.has(key),
  );
  const incompleteFormulaMetricKeys = requiredCalculatedMetricKeys.filter(
    (key) => metricIndex.get(key)?.status !== "Calculated",
  );

  return {
    ready:
      missingCoreMetricKeys.length === 0 &&
      incompleteFormulaMetricKeys.length === 0,
    coreDirectMetricCount: coreDirectMetricKeys.length,
    coreDirectAvailableCount: coreDirectMetricKeys.length - missingCoreMetricKeys.length,
    requiredCalculatedMetricCount: requiredCalculatedMetricKeys.length,
    requiredCalculatedMetricReadyCount:
      requiredCalculatedMetricKeys.length - incompleteFormulaMetricKeys.length,
    missingCoreMetricKeys: [...missingCoreMetricKeys],
    incompleteFormulaMetricKeys: [...incompleteFormulaMetricKeys],
  };
}

export function buildDatabookMetricsFromFormulaInputs(formulaInputs: FormulaInputAssignment[]) {
  const primaryPeriod = getPrimaryPeriod(formulaInputs.map((input) => input.period));

  const revenueInputs = getApprovedInputsByLineKey(formulaInputs, "revenue");
  const cogsInputs = getApprovedInputsByLineKey(formulaInputs, "cogs");
  const providedGrossProfitInputs = getApprovedInputsByLineKey(formulaInputs, "gross_profit");
  const opexInputs = getApprovedInputsByLineKey(formulaInputs, "operating_expenses");
  const providedEbitdaInputs = getApprovedInputsByLineKey(formulaInputs, "ebitda");
  const arrInputs = getApprovedInputsByLineKey(formulaInputs, "arr");
  const netRevenueRetentionInputs = getApprovedInputsByLineKey(formulaInputs, "net_revenue_retention");
  const churnInputs = getApprovedInputsByLineKey(formulaInputs, "customer_churn");
  const headcountInputs = getApprovedInputsByLineKey(formulaInputs, "headcount");
  const capexInputs = getApprovedInputsByLineKey(formulaInputs, "capex");

  const revenue = sumInputs(revenueInputs);
  const cogs = sumInputs(cogsInputs);
  const providedGrossProfit = sumInputs(providedGrossProfitInputs);
  const operatingExpenses = sumInputs(opexInputs);
  const providedEbitda = sumInputs(providedEbitdaInputs);
  const grossProfitCalculated = revenue !== null && cogs !== null;
  const ebitdaCalculated =
    grossProfitCalculated && operatingExpenses !== null;
  const grossProfit = grossProfitCalculated ? revenue - cogs : providedGrossProfit;
  const ebitda =
    ebitdaCalculated && grossProfit !== null && operatingExpenses !== null
      ? grossProfit - operatingExpenses
      : providedEbitda;
  const grossMargin =
    grossProfit !== null && revenue !== null && revenue !== 0 ? grossProfit / revenue : null;
  const ebitdaMargin =
    ebitda !== null && revenue !== null && revenue !== 0 ? ebitda / revenue : null;

  return [
    buildMetric({
      key: "revenue",
      outputLineKey: "revenue",
      label: "Revenue",
      period: primaryPeriod,
      value: revenue,
      status: revenueInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned Revenue inputs",
      definition: "Top-line revenue carried from approved source-backed rows.",
      rationale: "Revenue is taken directly from approved formula inputs assigned to the Revenue line.",
      calculationType: revenueInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: revenueInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(revenueInputs, "Missing approved Revenue inputs"),
      traceabilityStatus: getTraceabilityStatus(revenueInputs),
      format: "currency",
    }),
    buildMetric({
      key: "cogs",
      outputLineKey: "cogs",
      label: "COGS",
      period: primaryPeriod,
      value: cogs,
      status: cogsInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned COGS inputs",
      definition: "Direct delivery cost rows carried from approved source-backed items.",
      rationale: "COGS is aggregated from approved formula inputs assigned into the COGS line.",
      calculationType: cogsInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: cogsInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(cogsInputs, "Missing approved COGS inputs"),
      traceabilityStatus: getTraceabilityStatus(cogsInputs),
      format: "currency",
    }),
    buildMetric({
      key: "gross-profit",
      outputLineKey: "gross_profit",
      label: "Gross Profit",
      period: primaryPeriod,
      value: grossProfit,
      status:
        revenue !== null && cogs !== null
          ? "Calculated"
          : providedGrossProfitInputs.length > 0
            ? "Provided"
            : "Unavailable",
      formula:
        revenue !== null && cogs !== null
          ? "revenue - cogs"
          : "Assigned reported Gross Profit inputs",
      definition: "Gross Profit equals Revenue less COGS.",
      rationale:
        grossProfitCalculated
          ? "Gross Profit is calculated deterministically from standardized Revenue and COGS formula inputs."
          : providedGrossProfitInputs.length > 0
            ? "A reported Gross Profit row exists, but the final databook formula has not been completed because Revenue and COGS inputs are still incomplete."
            : "Gross Profit cannot be finalized until both Revenue and COGS inputs are available.",
      calculationType:
        grossProfitCalculated
          ? "Formula"
          : providedGrossProfitInputs.length > 1
            ? "Aggregation"
            : "Source Reported",
      directOrDerived: "Derived",
      formulaDependencies: ["Revenue", "COGS"],
      sourceInputIds:
        grossProfitCalculated
          ? [...revenueInputs, ...cogsInputs].map((input) => input.id)
          : providedGrossProfitInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(
        grossProfitCalculated
          ? [...revenueInputs, ...cogsInputs]
          : providedGrossProfitInputs,
        "Missing inputs for Gross Profit",
        grossProfitCalculated
          ? { derived: true, formula: "Revenue - COGS" }
          : undefined,
      ),
      traceabilityStatus: getTraceabilityStatus(
        grossProfitCalculated
          ? [...revenueInputs, ...cogsInputs]
          : providedGrossProfitInputs,
      ),
      format: "currency",
    }),
    buildMetric({
      key: "gross-margin",
      outputLineKey: "gross_margin",
      label: "Gross Margin",
      period: primaryPeriod,
      value: grossMargin,
      status: grossMargin !== null ? "Calculated" : "Unavailable",
      formula: "gross_profit / revenue",
      definition: "Gross Margin equals Gross Profit divided by Revenue.",
      rationale:
        grossMargin !== null
          ? "Gross Margin is calculated deterministically from standardized Gross Profit and Revenue outputs."
          : "Gross Margin cannot be calculated until both Revenue and Gross Profit are available.",
      calculationType: "Ratio",
      directOrDerived: "Derived",
      formulaDependencies: ["Gross Profit", "Revenue"],
      sourceInputIds: [...revenueInputs, ...cogsInputs].map((input) => input.id),
      sourceSummary: buildSourceSummary(
        [...revenueInputs, ...cogsInputs],
        "Missing inputs for Gross Margin",
        { derived: true, formula: "Gross Profit / Revenue" },
      ),
      traceabilityStatus: getTraceabilityStatus([...revenueInputs, ...cogsInputs]),
      format: "percentage",
    }),
    buildMetric({
      key: "operating-expenses",
      outputLineKey: "operating_expenses",
      label: "Operating Expenses",
      period: primaryPeriod,
      value: operatingExpenses,
      status: opexInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned Operating Expenses inputs",
      definition: "Operating expense rows carried from approved source-backed items.",
      rationale: "Operating Expenses are aggregated from approved formula inputs assigned to the OpEx line.",
      calculationType: opexInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: opexInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(opexInputs, "Missing approved Operating Expenses inputs"),
      traceabilityStatus: getTraceabilityStatus(opexInputs),
      format: "currency",
    }),
    buildMetric({
      key: "ebitda",
      outputLineKey: "ebitda",
      label: "EBITDA",
      period: primaryPeriod,
      value: ebitda,
      status:
        grossProfit !== null && operatingExpenses !== null
          ? "Calculated"
          : providedEbitdaInputs.length > 0
            ? "Provided"
            : "Unavailable",
      formula:
        grossProfit !== null && operatingExpenses !== null
          ? "gross_profit - operating_expenses"
          : "Assigned reported EBITDA inputs",
      definition: "EBITDA equals Gross Profit less Operating Expenses.",
      rationale:
        ebitdaCalculated
          ? "EBITDA is calculated deterministically from standardized Gross Profit and Operating Expenses inputs."
          : providedEbitdaInputs.length > 0
            ? "A reported EBITDA row exists, but the final databook formula has not been completed because Gross Profit or Operating Expenses inputs are still incomplete."
            : "EBITDA cannot be finalized until Gross Profit and Operating Expenses are available.",
      calculationType:
        ebitdaCalculated
          ? "Formula"
          : providedEbitdaInputs.length > 1
            ? "Aggregation"
            : "Source Reported",
      directOrDerived: "Derived",
      formulaDependencies: ["Gross Profit", "Operating Expenses"],
      sourceInputIds:
        ebitdaCalculated
          ? [...revenueInputs, ...cogsInputs, ...opexInputs].map((input) => input.id)
          : providedEbitdaInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(
        ebitdaCalculated
          ? [...revenueInputs, ...cogsInputs, ...opexInputs]
          : providedEbitdaInputs,
        "Missing inputs for EBITDA",
        ebitdaCalculated
          ? { derived: true, formula: "Gross Profit - Operating Expenses" }
          : undefined,
      ),
      traceabilityStatus: getTraceabilityStatus(
        ebitdaCalculated
          ? [...revenueInputs, ...cogsInputs, ...opexInputs]
          : providedEbitdaInputs,
      ),
      format: "currency",
    }),
    buildMetric({
      key: "ebitda-margin",
      outputLineKey: "ebitda_margin",
      label: "EBITDA Margin",
      period: primaryPeriod,
      value: ebitdaMargin,
      status: ebitdaMargin !== null ? "Calculated" : "Unavailable",
      formula: "ebitda / revenue",
      definition: "EBITDA Margin equals EBITDA divided by Revenue.",
      rationale:
        ebitdaMargin !== null
          ? "EBITDA Margin is calculated deterministically from standardized EBITDA and Revenue outputs."
          : "EBITDA Margin cannot be calculated until both EBITDA and Revenue are available.",
      calculationType: "Ratio",
      directOrDerived: "Derived",
      formulaDependencies: ["EBITDA", "Revenue"],
      sourceInputIds: [...revenueInputs, ...cogsInputs, ...opexInputs].map((input) => input.id),
      sourceSummary: buildSourceSummary(
        [...revenueInputs, ...cogsInputs, ...opexInputs],
        "Missing inputs for EBITDA Margin",
        { derived: true, formula: "EBITDA / Revenue" },
      ),
      traceabilityStatus: getTraceabilityStatus([...revenueInputs, ...cogsInputs, ...opexInputs]),
      format: "percentage",
    }),
    buildMetric({
      key: "arr",
      outputLineKey: "arr",
      label: "ARR",
      period: primaryPeriod,
      value: sumInputs(arrInputs),
      status: arrInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned ARR inputs",
      definition: "Annual recurring revenue carried from approved recurring revenue inputs.",
      rationale: "ARR remains direct unless a future definition provider explicitly assigns a derived ARR formula family.",
      calculationType: arrInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: arrInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(arrInputs, "Missing approved ARR inputs"),
      traceabilityStatus: getTraceabilityStatus(arrInputs),
      format: "currency",
    }),
    buildMetric({
      key: "net-revenue-retention",
      outputLineKey: "net_revenue_retention",
      label: "Net Revenue Retention",
      period: primaryPeriod,
      value: firstInputValue(netRevenueRetentionInputs),
      status: netRevenueRetentionInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned Net Revenue Retention inputs",
      definition: "Net revenue retention percentage carried from approved source-backed KPI rows.",
      rationale:
        "Net Revenue Retention is taken directly from approved KPI inputs and kept separate from the core Revenue line.",
      calculationType: "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: netRevenueRetentionInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(
        netRevenueRetentionInputs,
        "Missing approved Net Revenue Retention inputs",
      ),
      traceabilityStatus: getTraceabilityStatus(netRevenueRetentionInputs),
      format: "percentage",
    }),
    buildMetric({
      key: "customer-churn",
      outputLineKey: "customer_churn",
      label: "Customer Churn",
      period: primaryPeriod,
      value: firstInputValue(churnInputs),
      status: churnInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned Customer Churn input",
      definition: "Customer churn percentage carried from approved KPI rows.",
      rationale: "Churn is kept as a direct KPI instead of being recomputed from incomplete supporting cohorts.",
      calculationType: "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: churnInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(churnInputs, "Missing approved churn input"),
      traceabilityStatus: getTraceabilityStatus(churnInputs),
      format: "percentage",
    }),
    buildMetric({
      key: "headcount",
      outputLineKey: "headcount",
      label: "Headcount",
      period: primaryPeriod,
      value: sumInputs(headcountInputs),
      status: headcountInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned Headcount inputs",
      definition: "Headcount carried from approved workforce rows.",
      rationale: "Headcount is aggregated from approved formula inputs such as department-level FTE counts.",
      calculationType: headcountInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: headcountInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(headcountInputs, "Missing approved headcount input"),
      traceabilityStatus: getTraceabilityStatus(headcountInputs),
      format: "number",
    }),
    buildMetric({
      key: "capex",
      outputLineKey: "capex",
      label: "CapEx",
      period: primaryPeriod,
      value: sumInputs(capexInputs),
      status: capexInputs.length > 0 ? "Provided" : "Unavailable",
      formula: "Assigned CapEx inputs",
      definition: "Capital expenditure carried from approved source-backed investment rows.",
      rationale: "CapEx is aggregated from approved direct formula inputs tagged to the CapEx output line.",
      calculationType: capexInputs.length > 1 ? "Aggregation" : "Source Reported",
      directOrDerived: "Direct",
      formulaDependencies: [],
      sourceInputIds: capexInputs.map((input) => input.id),
      sourceSummary: buildSourceSummary(capexInputs, "Missing approved CapEx inputs"),
      traceabilityStatus: getTraceabilityStatus(capexInputs),
      format: "currency",
    }),
  ] satisfies DatabookMetricRecord[];
}
