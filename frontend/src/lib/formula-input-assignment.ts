import { normalizeLabel } from "@/lib/dataroom-utils";
import { DefinedItem, FormulaInputAssignment, FormulaRole, FormulaTemplateKey } from "@/lib/types";

interface FormulaAssignmentSpec {
  outputLineKey: string;
  formulaRole: FormulaRole;
  formulaTemplateKey: FormulaTemplateKey;
  dependencyCandidates: string[];
}

const formulaAssignmentSpecs: Record<string, FormulaAssignmentSpec> = {
  Revenue: {
    outputLineKey: "revenue",
    formulaRole: "Input",
    formulaTemplateKey: "revenue",
    dependencyCandidates: [],
  },
  COGS: {
    outputLineKey: "cogs",
    formulaRole: "Input",
    formulaTemplateKey: "cogs",
    dependencyCandidates: [],
  },
  "Gross Profit": {
    outputLineKey: "gross_profit",
    formulaRole: "Reported Metric",
    formulaTemplateKey: "gross_profit",
    dependencyCandidates: ["revenue", "cogs"],
  },
  "Operating Expenses": {
    outputLineKey: "operating_expenses",
    formulaRole: "Input",
    formulaTemplateKey: "operating_expenses",
    dependencyCandidates: [],
  },
  EBITDA: {
    outputLineKey: "ebitda",
    formulaRole: "Reported Metric",
    formulaTemplateKey: "ebitda",
    dependencyCandidates: ["gross_profit", "operating_expenses"],
  },
  ARR: {
    outputLineKey: "arr",
    formulaRole: "Input",
    formulaTemplateKey: "arr",
    dependencyCandidates: [],
  },
  "Net Revenue Retention": {
    outputLineKey: "net_revenue_retention",
    formulaRole: "Input",
    formulaTemplateKey: "net_revenue_retention",
    dependencyCandidates: [],
  },
  "Customer Churn": {
    outputLineKey: "customer_churn",
    formulaRole: "Input",
    formulaTemplateKey: "customer_churn",
    dependencyCandidates: [],
  },
  Headcount: {
    outputLineKey: "headcount",
    formulaRole: "Input",
    formulaTemplateKey: "headcount",
    dependencyCandidates: [],
  },
  CapEx: {
    outputLineKey: "capex",
    formulaRole: "Input",
    formulaTemplateKey: "capex",
    dependencyCandidates: [],
  },
  Unmapped: {
    outputLineKey: "unmapped",
    formulaRole: "Review",
    formulaTemplateKey: "none",
    dependencyCandidates: [],
  },
};

export function getFormulaAssignmentSpec(mappedMetric: string) {
  return formulaAssignmentSpecs[mappedMetric] ?? formulaAssignmentSpecs.Unmapped;
}

type SelectionMode = "single-best" | "prefer-total-else-components";

interface MetricAssignmentStrategy {
  outputLineKey: string;
  selectionMode: SelectionMode;
  acceptedUnits: string[];
  exactLabels: string[];
  componentKeywords: string[];
  preferredSheetKeywords: string[];
  rejectLabelKeywords?: string[];
}

interface CandidateScore {
  definedItemId: string;
  eligible: boolean;
  exactMatch: boolean;
  componentMatch: boolean;
  score: number;
}

const metricAssignmentStrategies: Record<string, MetricAssignmentStrategy> = {
  Revenue: {
    outputLineKey: "revenue",
    selectionMode: "prefer-total-else-components",
    acceptedUnits: ["USD"],
    exactLabels: ["revenue", "total revenue", "net revenue"],
    componentKeywords: ["subscription revenue", "services revenue", "license revenue"],
    preferredSheetKeywords: ["p l", "income statement", "qoe monthly p l", "monthly p l"],
  },
  COGS: {
    outputLineKey: "cogs",
    selectionMode: "prefer-total-else-components",
    acceptedUnits: ["USD"],
    exactLabels: ["cogs", "cost of revenue", "cost of sales", "cost of goods sold"],
    componentKeywords: [
      "delivery labor",
      "implementation labor",
      "hosting",
      "cloud infrastructure",
      "merchant fees",
      "support labor",
      "cost of revenue",
    ],
    preferredSheetKeywords: ["p l", "income statement", "qoe monthly p l", "monthly p l"],
  },
  "Gross Profit": {
    outputLineKey: "gross_profit",
    selectionMode: "single-best",
    acceptedUnits: ["USD"],
    exactLabels: ["gross profit"],
    componentKeywords: ["gross profit", "adjusted gross profit"],
    preferredSheetKeywords: ["p l", "income statement", "qoe monthly p l", "monthly p l"],
  },
  "Operating Expenses": {
    outputLineKey: "operating_expenses",
    selectionMode: "prefer-total-else-components",
    acceptedUnits: ["USD"],
    exactLabels: ["operating expenses", "opex", "sg a"],
    componentKeywords: [
      "sales marketing",
      "s m",
      "g a",
      "general and administrative",
      "general administrative",
      "research development",
      "r d",
      "operating expenses",
    ],
    preferredSheetKeywords: ["p l", "income statement", "qoe monthly p l", "monthly p l"],
  },
  EBITDA: {
    outputLineKey: "ebitda",
    selectionMode: "single-best",
    acceptedUnits: ["USD"],
    exactLabels: ["ebitda", "adjusted ebitda"],
    componentKeywords: ["ebitda", "adjusted ebitda"],
    preferredSheetKeywords: ["p l", "income statement", "qoe monthly p l", "monthly p l", "qoe"],
  },
  ARR: {
    outputLineKey: "arr",
    selectionMode: "single-best",
    acceptedUnits: ["USD"],
    exactLabels: ["arr", "annual recurring revenue"],
    componentKeywords: ["arr", "annual recurring revenue"],
    preferredSheetKeywords: ["operating kpis", "kpi", "arr"],
  },
  "Net Revenue Retention": {
    outputLineKey: "net_revenue_retention",
    selectionMode: "single-best",
    acceptedUnits: ["%"],
    exactLabels: [
      "net revenue retention",
      "nrr",
      "net dollar retention",
      "gross revenue retention",
      "grr",
    ],
    componentKeywords: [
      "net revenue retention",
      "nrr",
      "net dollar retention",
      "gross revenue retention",
      "grr",
      "retention",
    ],
    preferredSheetKeywords: ["operating kpis", "kpi", "retention", "cohort"],
  },
  "Customer Churn": {
    outputLineKey: "customer_churn",
    selectionMode: "single-best",
    acceptedUnits: ["%"],
    exactLabels: ["customer churn", "logo churn", "revenue churn", "churn"],
    componentKeywords: ["customer churn", "logo churn", "revenue churn", "churn"],
    preferredSheetKeywords: ["operating kpis", "kpi", "churn"],
  },
  Headcount: {
    outputLineKey: "headcount",
    selectionMode: "prefer-total-else-components",
    acceptedUnits: ["count"],
    exactLabels: ["headcount", "total headcount", "employee count", "total employees", "total fte"],
    componentKeywords: ["headcount", "employee count", "fte"],
    preferredSheetKeywords: ["headcount", "workforce", "operating kpis"],
    rejectLabelKeywords: ["per employee", "revenue per employee", "capex per employee", "salary", "compensation"],
  },
  CapEx: {
    outputLineKey: "capex",
    selectionMode: "prefer-total-else-components",
    acceptedUnits: ["USD"],
    exactLabels: ["capex", "capital expenditures", "capital expenditure"],
    componentKeywords: ["capex", "capital expenditures", "capital expenditure"],
    preferredSheetKeywords: ["capex", "cash flow", "headcount capex", "operating kpis"],
  },
};

function buildMatchPatterns(values: string[]) {
  return values.map((value) => normalizeLabel(value));
}

function scoreDefinedItemForAssignment(
  item: DefinedItem,
  strategy: MetricAssignmentStrategy,
): CandidateScore {
  const label = normalizeLabel(item.rawLabel);
  const context = normalizeLabel(`${item.sourceSheetName} ${item.sourceFileName}`);
  const exactPatterns = buildMatchPatterns(strategy.exactLabels);
  const componentPatterns = buildMatchPatterns(strategy.componentKeywords);
  const preferredSheetPatterns = buildMatchPatterns(strategy.preferredSheetKeywords);
  const rejectPatterns = buildMatchPatterns(strategy.rejectLabelKeywords ?? []);

  if (
    item.outputLineKey !== strategy.outputLineKey ||
    item.normalizedValue === null ||
    !strategy.acceptedUnits.includes(item.unit)
  ) {
    return {
      definedItemId: item.id,
      eligible: false,
      exactMatch: false,
      componentMatch: false,
      score: 0,
    };
  }

  if (rejectPatterns.some((pattern) => pattern && label.includes(pattern))) {
    return {
      definedItemId: item.id,
      eligible: false,
      exactMatch: false,
      componentMatch: false,
      score: 0,
    };
  }

  const exactMatch = exactPatterns.some(
    (pattern) => pattern && (label === pattern || label.endsWith(pattern)),
  );
  const componentMatch = componentPatterns.some(
    (pattern) => pattern && label.includes(pattern),
  );
  const preferredContextMatch = preferredSheetPatterns.some(
    (pattern) => pattern && context.includes(pattern),
  );

  if (!exactMatch && !componentMatch) {
    return {
      definedItemId: item.id,
      eligible: false,
      exactMatch: false,
      componentMatch: false,
      score: 0,
    };
  }

  let score = 0;

  if (exactMatch) {
    score += 100;
  } else if (componentMatch) {
    score += 76;
  }

  if (preferredContextMatch) {
    score += 12;
  }

  if (item.reviewStatus === "Approved" || item.reviewStatus === "Rule Applied") {
    score += 10;
  } else if (item.reviewStatus === "Pending") {
    score -= 10;
  } else if (item.reviewStatus === "Flagged") {
    score -= 18;
  }

  if (item.traceabilityStatus === "Traced") {
    score += 6;
  } else if (item.traceabilityStatus === "Missing") {
    score -= 18;
  }

  return {
    definedItemId: item.id,
    eligible: score >= 70,
    exactMatch,
    componentMatch,
    score,
  };
}

function dedupeBestCandidates(
  items: DefinedItem[],
  candidateScores: CandidateScore[],
) {
  const bestByLabel = new Map<string, CandidateScore>();
  const itemIndex = new Map(items.map((item) => [item.id, item]));

  for (const candidate of candidateScores) {
    const item = itemIndex.get(candidate.definedItemId);
    const normalizedLabel = normalizeLabel(item?.rawLabel ?? candidate.definedItemId);
    const previous = bestByLabel.get(normalizedLabel);

    if (!previous || candidate.score > previous.score) {
      bestByLabel.set(normalizedLabel, candidate);
    }
  }

  return [...bestByLabel.values()];
}

function pickAssignedItemsForMetric(
  metric: string,
  items: DefinedItem[],
) {
  const strategy = metricAssignmentStrategies[metric];

  if (!strategy) {
    return new Set<string>();
  }

  const candidates = dedupeBestCandidates(
    items,
    items
      .map((item) => scoreDefinedItemForAssignment(item, strategy))
      .filter((candidate) => candidate.eligible),
  ).sort((left, right) => right.score - left.score);

  if (candidates.length === 0) {
    return new Set<string>();
  }

  if (strategy.selectionMode === "single-best") {
    return new Set([candidates[0].definedItemId]);
  }

  const exactCandidates = candidates.filter((candidate) => candidate.exactMatch);

  if (exactCandidates.length > 0) {
    return new Set([exactCandidates[0].definedItemId]);
  }

  const componentCandidates = candidates.filter((candidate) => candidate.componentMatch);

  return new Set(
    (componentCandidates.length > 0 ? componentCandidates : candidates).map(
      (candidate) => candidate.definedItemId,
    ),
  );
}

export function buildFormulaInputAssignments(definedItems: DefinedItem[]) {
  const coreDefinedItems = definedItems.filter((item) => item.entersCorePipeline !== false);
  const assignedDefinedItemIds = new Set<string>();

  Object.keys(metricAssignmentStrategies).forEach((metric) => {
    pickAssignedItemsForMetric(metric, coreDefinedItems).forEach((itemId) => {
      assignedDefinedItemIds.add(itemId);
    });
  });

  return coreDefinedItems.map((item) => ({
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
    assignedValue:
      !assignedDefinedItemIds.has(item.id) ||
      item.reviewStatus === "Flagged" ||
      item.traceabilityStatus === "Missing"
        ? null
        : item.normalizedValue,
    unit: item.unit,
    rationale: item.rationale,
    reviewStatus: item.reviewStatus,
    traceabilityStatus: item.traceabilityStatus,
  } satisfies FormulaInputAssignment));
}
