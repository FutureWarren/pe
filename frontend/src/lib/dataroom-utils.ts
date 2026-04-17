import { DatabookMetricRecord } from "@/lib/types";

export function normalizeLabel(value: string) {
  return value.trim().toLowerCase().replaceAll(/[^a-z0-9]+/g, " ").trim();
}

export function isNumericLike(value: string) {
  const cleaned = value.replaceAll(",", "").trim().toLowerCase();

  return /^-?\(?\$?\d+(\.\d+)?[kmb]?%?\)?$/i.test(cleaned);
}

export function parseScaledFinancialValue(value?: string) {
  if (!value) {
    return null;
  }

  const normalized = value.replaceAll(",", "").trim().toLowerCase();
  const multiplier = normalized.endsWith("b")
    ? 1_000_000_000
    : normalized.endsWith("m")
      ? 1_000_000
      : normalized.endsWith("k")
        ? 1_000
        : 1;
  const numeric = Number(normalized.replaceAll(/[^0-9.-]+/g, ""));

  if (Number.isNaN(numeric)) {
    return null;
  }

  return numeric * multiplier;
}

export function detectUnit(params: {
  rawValue: string;
  rawLabel: string;
  mappedCategory?: string;
}) {
  const normalizedLabel = normalizeLabel(
    `${params.mappedCategory ?? ""} ${params.rawLabel}`,
  );

  if (params.rawValue.includes("%") || normalizedLabel.includes("margin") || normalizedLabel.includes("churn")) {
    return "%";
  }

  if (
    normalizedLabel.includes("headcount") ||
    normalizedLabel.includes("fte") ||
    normalizedLabel.includes("employee")
  ) {
    return "count";
  }

  if (parseScaledFinancialValue(params.rawValue) !== null) {
    return "USD";
  }

  return "unknown";
}

export function normalizeValueForUnit(rawValue: string, unit: string) {
  const parsed = parseScaledFinancialValue(rawValue);

  if (parsed === null) {
    return null;
  }

  if (unit === "%") {
    return Math.abs(parsed) > 1 ? parsed / 100 : parsed;
  }

  return parsed;
}

export function formatMetricValue(
  value: number | null,
  format: DatabookMetricRecord["format"],
) {
  if (value === null) {
    return "Unavailable";
  }

  if (format === "percentage") {
    return `${(value * 100).toFixed(1)}%`;
  }

  if (format === "number") {
    return new Intl.NumberFormat("en-US", {
      maximumFractionDigits: 0,
    }).format(value);
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: Math.abs(value) >= 1_000_000 ? 0 : 2,
  }).format(value);
}

export function parseSourceLocator(sourceLocator: string) {
  const [sheetCandidate, cellOrRow] = sourceLocator.includes("!")
    ? sourceLocator.split("!")
    : ["Imported file", sourceLocator];

  return {
    sourceSheetName: sheetCandidate || "Imported file",
    sourceLocation: sourceLocator || "Not captured",
    cellOrRow: cellOrRow || sourceLocator || "Not captured",
  };
}

export function dedupeStrings(values: string[]) {
  return values.filter((value, index, array) => value && array.indexOf(value) === index);
}

export function getPrimaryPeriod(periods: string[]) {
  if (periods.length === 0) {
    return "Current Period";
  }

  const counts = new Map<string, number>();

  for (const period of periods) {
    counts.set(period, (counts.get(period) ?? 0) + 1);
  }

  return [...counts.entries()].sort((left, right) => right[1] - left[1])[0]?.[0] ?? "Current Period";
}
