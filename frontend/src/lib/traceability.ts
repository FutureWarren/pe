import { DatabookMetricRecord, FormulaInputAssignment, TraceabilityRecord } from "@/lib/types";

function createTraceId(metricKey: string, sourceItemId?: string, index = 0) {
  return sourceItemId
    ? `trace-${metricKey}-${sourceItemId}`
    : `trace-${metricKey}-missing-${index}`;
}

export function buildTraceabilityRecords(
  metrics: DatabookMetricRecord[],
  formulaInputs: FormulaInputAssignment[],
) {
  const formulaInputIndex = new Map(formulaInputs.map((input) => [input.id, input]));
  const records: TraceabilityRecord[] = [];

  for (const metric of metrics) {
    if (metric.sourceInputIds.length === 0) {
      records.push({
        id: createTraceId(metric.key, undefined, records.length),
        outputMetricKey: metric.key,
        outputLineKey: metric.outputLineKey,
        outputMetricLabel: metric.label,
        outputMetricValue: metric.formattedValue,
        sourceFileName: "No source item captured",
        sourceSheetName: "Not available",
        sourceLocation: "Not available",
        rawLabel: metric.label,
        rawValue: metric.formattedValue,
        period: metric.period,
        mappedCategory: metric.label,
        directOrDerived: metric.directOrDerived,
        formulaDependencies: metric.formulaDependencies,
        derivationPath: metric.formula,
        traceabilityStatus: metric.traceabilityStatus,
      });
      continue;
    }

    for (const sourceInputId of metric.sourceInputIds) {
      const sourceInput = formulaInputIndex.get(sourceInputId);

      records.push({
        id: createTraceId(metric.key, sourceInputId, records.length),
        outputMetricKey: metric.key,
        outputLineKey: metric.outputLineKey,
        outputMetricLabel: metric.label,
        outputMetricValue: metric.formattedValue,
        sourceInputId,
        definedItemId: sourceInput?.definedItemId,
        sourceFileId: sourceInput?.sourceFileId,
        sourceFileName: sourceInput?.sourceFileName ?? "Unknown file",
        sourceSheetName: sourceInput?.sourceSheetName ?? "Unknown sheet",
        sourceLocation: sourceInput?.sourceLocation ?? "Unknown location",
        rawLabel: sourceInput?.rawLabel ?? metric.label,
        rawValue: sourceInput?.rawValue ?? metric.formattedValue,
        period: sourceInput?.period ?? metric.period,
        mappedCategory: sourceInput?.mappedMetric ?? metric.label,
        directOrDerived: metric.directOrDerived,
        formulaDependencies: metric.formulaDependencies,
        derivationPath:
          metric.status === "Calculated"
            ? `${metric.label} = ${metric.formula}`
            : "Direct source-backed databook metric",
        traceabilityStatus:
          sourceInput?.traceabilityStatus ?? metric.traceabilityStatus,
      });
    }
  }

  return records;
}
