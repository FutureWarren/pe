import * as XLSX from "xlsx";

import { dedupeStrings } from "@/lib/dataroom-utils";
import { Deal, DefinedItem, FormulaInputAssignment } from "@/lib/types";

function getFileStub(value: string) {
  return value
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-|-$/g, "");
}

function getMetricRowNumber(index: number) {
  return 5 + index;
}

function buildDirectInputFormula(lineKey: string, aggregation: "sum" | "average" = "sum") {
  const aggregateExpression =
    aggregation === "average"
      ? `AVERAGEIFS(Formula_Inputs!$J:$J,Formula_Inputs!$D:$D,"${lineKey}",Formula_Inputs!$J:$J,"<>")`
      : `SUMIFS(Formula_Inputs!$J:$J,Formula_Inputs!$D:$D,"${lineKey}",Formula_Inputs!$J:$J,"<>")`;

  return `IF(COUNTIFS(Formula_Inputs!$D:$D,"${lineKey}",Formula_Inputs!$J:$J,"<>")=0,"",${aggregateExpression})`;
}

function getMetricFormat(format: "currency" | "percentage" | "number") {
  if (format === "percentage") {
    return "0.0%";
  }

  if (format === "number") {
    return "#,##0";
  }

  return "$#,##0_);($#,##0)";
}

function flattenSourceRows(deal: Deal) {
  const extractedRows = deal.extractedItems.flatMap((item) =>
    (item.rows ?? []).map((row) => ({
      source_file:
        deal.sourceFiles.find((file) => file.id === item.sourceFileId)?.name ?? "Unknown file",
      source_table: item.tableName ?? item.title,
      period: item.period,
      location: row.location,
      raw_label: row.label,
      raw_value: row.value,
      raw_cells: row.rawCells.join(" | "),
      issue_flags: item.issueFlags.join(" | "),
    })),
  );

  if (extractedRows.length > 0) {
    return extractedRows;
  }

  return deal.mappingRows.map((row) => ({
    source_file:
      deal.sourceFiles.find((file) => file.id === row.sourceFileId)?.name ?? "Unknown file",
    source_table: "Mapped source row",
    period: row.period,
    location: row.sourceLocator,
    raw_label: row.rawLineItemLabel,
    raw_value: row.rawValue,
    raw_cells: "",
    issue_flags: row.status === "Needs Review" ? "Needs Review" : "None",
  }));
}

function buildDefinitionsSheetRows(definedItems: DefinedItem[]) {
  return definedItems.map((item) => ({
    id: item.id,
    source_file: item.sourceFileName,
    source_sheet: item.sourceSheetName,
    source_location: item.sourceLocation,
    period: item.period,
    raw_label: item.rawLabel,
    raw_value: item.rawValue,
    normalized_value: item.normalizedValue ?? "",
    unit: item.unit,
    detected_type: item.detectedType,
    mapped_category: item.mappedCategory,
    mapped_metric: item.mappedMetric,
    output_line_key: item.outputLineKey,
    formula_role: item.formulaRole,
    dependency_candidates: item.dependencyCandidates.join(" | "),
    formula_template_key: item.formulaTemplateKey,
    definition: item.definition,
    rationale: item.rationale,
    calculation_type: item.calculationType,
    direct_or_derived: item.directOrDerived,
    formula_dependencies: item.formulaDependencies.join(" | "),
    review_status: item.reviewStatus,
    traceability_status: item.traceabilityStatus,
  }));
}

function buildFormulaInputsSheetRows(formulaInputs: FormulaInputAssignment[]) {
  return formulaInputs.map((input) => ({
    id: input.id,
    defined_item_id: input.definedItemId,
    mapped_metric: input.mappedMetric,
    output_line_key: input.outputLineKey,
    formula_role: input.formulaRole,
    formula_template_key: input.formulaTemplateKey,
    dependency_candidates: input.dependencyCandidates.join(" | "),
    period: input.period,
    normalized_value: input.normalizedValue ?? "",
    assigned_value: input.assignedValue ?? "",
    unit: input.unit,
    direct_or_derived: input.directOrDerived,
    review_status: input.reviewStatus,
    traceability_status: input.traceabilityStatus,
    source_file: input.sourceFileName,
    source_sheet: input.sourceSheetName,
    source_location: input.sourceLocation,
    raw_label: input.rawLabel,
    raw_value: input.rawValue,
    rationale: input.rationale,
  }));
}

function buildDatabookSheet(deal: Deal) {
  const metrics = deal.databookMetrics ?? [];
  const periodLabel = metrics[0]?.period ?? "Current Period";
  const sheet = XLSX.utils.aoa_to_sheet([
    ["Angelic Dataroom", deal.targetCompanyName],
    ["Generated", new Date().toISOString()],
    [],
    ["Output Line Key", "Metric", periodLabel, "Status", "Direct / Derived", "Definition", "Formula / Trace"],
    ...metrics.map((metric) => [
      metric.outputLineKey,
      metric.label,
      metric.status === "Unavailable" ? "" : metric.value,
      metric.status,
      metric.directOrDerived,
      metric.definition,
      metric.sourceSummary,
    ]),
  ]);

  const rowNumbers = new Map(metrics.map((metric, index) => [metric.key, getMetricRowNumber(index)]));
  const directFormulaByKey: Partial<Record<string, string>> = {
    revenue: buildDirectInputFormula("revenue"),
    cogs: buildDirectInputFormula("cogs"),
    "operating-expenses": buildDirectInputFormula("operating_expenses"),
    arr: buildDirectInputFormula("arr"),
    "net-revenue-retention": buildDirectInputFormula("net_revenue_retention", "average"),
    "customer-churn": buildDirectInputFormula("customer_churn", "average"),
    headcount: buildDirectInputFormula("headcount"),
    capex: buildDirectInputFormula("capex"),
    "gross-profit": buildDirectInputFormula("gross_profit"),
    ebitda: buildDirectInputFormula("ebitda"),
  };
  const formulaByKey: Partial<Record<string, string>> = {
    "gross-profit":
      rowNumbers.get("revenue") && rowNumbers.get("cogs")
        ? `C${rowNumbers.get("revenue")}-C${rowNumbers.get("cogs")}`
        : "",
    "gross-margin":
      rowNumbers.get("gross-profit") && rowNumbers.get("revenue")
        ? `IFERROR(C${rowNumbers.get("gross-profit")}/C${rowNumbers.get("revenue")},0)`
        : "",
    ebitda:
      rowNumbers.get("gross-profit") && rowNumbers.get("operating-expenses")
        ? `C${rowNumbers.get("gross-profit")}-C${rowNumbers.get("operating-expenses")}`
        : "",
    "ebitda-margin":
      rowNumbers.get("ebitda") && rowNumbers.get("revenue")
        ? `IFERROR(C${rowNumbers.get("ebitda")}/C${rowNumbers.get("revenue")},0)`
        : "",
  };

  for (const [index, metric] of metrics.entries()) {
    const rowNumber = getMetricRowNumber(index);
    const valueCell = `C${rowNumber}`;
    const formula =
      metric.status === "Calculated"
        ? formulaByKey[metric.key]
        : metric.status === "Provided"
          ? directFormulaByKey[metric.key]
          : "";

    if (formula) {
      sheet[valueCell] = {
        t: "n",
        f: formula,
        z: getMetricFormat(metric.format),
      };
    } else if (metric.value !== null) {
      sheet[valueCell] = {
        t: "n",
        v: metric.value,
        z: getMetricFormat(metric.format),
      };
    }
  }

  sheet["!cols"] = [
    { wch: 18 },
    { wch: 24 },
    { wch: 16 },
    { wch: 14 },
    { wch: 16 },
    { wch: 52 },
    { wch: 80 },
  ];

  return sheet;
}

function buildTraceabilitySheetRows(deal: Deal) {
  return (deal.traceabilityRecords ?? []).map((record) => ({
    output_line_key: record.outputLineKey,
    output_metric: record.outputMetricLabel,
    output_value: record.outputMetricValue,
    source_file: record.sourceFileName,
    source_sheet: record.sourceSheetName,
    source_location: record.sourceLocation,
    raw_label: record.rawLabel,
    raw_value: record.rawValue,
    period: record.period,
    mapped_category: record.mappedCategory,
    direct_or_derived: record.directOrDerived,
    formula_dependencies: record.formulaDependencies.join(" | "),
    derivation_path: record.derivationPath,
    traceability_status: record.traceabilityStatus,
  }));
}

function buildReviewFlagsSheetRows(deal: Deal) {
  return deal.exceptions
    .filter((item) => item.status === "Open")
    .map((item) => ({
      severity: item.severity,
      category: item.category,
      affected_line_item: item.affectedLineItem,
      detail: item.detail,
      suggested_resolution: item.suggestedResolution,
      status: item.status,
    }));
}

function applyStandardColumns(sheet: XLSX.WorkSheet, widths: number[]) {
  sheet["!cols"] = widths.map((width) => ({ wch: width }));
}

export function buildDatabookWorkbook(deal: Deal) {
  const workbook = XLSX.utils.book_new();
  const sourceRawSheet = XLSX.utils.json_to_sheet(flattenSourceRows(deal));
  const definitionsSheet = XLSX.utils.json_to_sheet(
    buildDefinitionsSheetRows(deal.definedItems ?? []),
  );
  const formulaInputsSheet = XLSX.utils.json_to_sheet(
    buildFormulaInputsSheetRows(deal.formulaInputs ?? []),
  );
  const databookSheet = buildDatabookSheet(deal);
  const traceabilitySheet = XLSX.utils.json_to_sheet(buildTraceabilitySheetRows(deal));
  const reviewFlags = buildReviewFlagsSheetRows(deal);

  applyStandardColumns(sourceRawSheet, [28, 24, 16, 18, 32, 18, 48, 20]);
  applyStandardColumns(definitionsSheet, [22, 26, 22, 18, 16, 26, 16, 16, 18, 18, 16, 18, 24, 18, 44, 56, 16, 16, 24, 16, 18]);
  applyStandardColumns(formulaInputsSheet, [22, 22, 18, 18, 16, 18, 24, 16, 16, 16, 12, 16, 14, 18, 26, 22, 18, 26, 16, 52]);
  applyStandardColumns(traceabilitySheet, [18, 22, 16, 26, 22, 18, 28, 16, 16, 18, 24, 56, 18]);

  XLSX.utils.book_append_sheet(workbook, sourceRawSheet, "Source_Raw");
  XLSX.utils.book_append_sheet(workbook, definitionsSheet, "Defined_Items");
  XLSX.utils.book_append_sheet(workbook, formulaInputsSheet, "Formula_Inputs");
  XLSX.utils.book_append_sheet(workbook, databookSheet, "Databook");
  XLSX.utils.book_append_sheet(workbook, traceabilitySheet, "Traceability");

  if (reviewFlags.length > 0) {
    const reviewSheet = XLSX.utils.json_to_sheet(reviewFlags);
    applyStandardColumns(reviewSheet, [12, 18, 28, 56, 42, 14]);
    XLSX.utils.book_append_sheet(workbook, reviewSheet, "Review_Flags");
  }

  workbook.Props = {
    Title: `${deal.targetCompanyName} databook`,
    Subject: "Definition-backed, traceable databook export",
    Author: "Angelic Dataroom",
    Company: "Angelic",
    Comments: `Files used: ${dedupeStrings(deal.sourceFiles.map((file) => file.name)).join(" | ")}`,
  };

  return workbook;
}

export function downloadDatabookWorkbook(deal: Deal) {
  const workbook = buildDatabookWorkbook(deal);
  XLSX.writeFile(workbook, `${getFileStub(deal.targetCompanyName)}-databook.xlsx`, {
    compression: true,
  });
}
