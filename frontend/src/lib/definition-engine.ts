import { dedupeStrings, detectUnit, normalizeLabel, normalizeValueForUnit, parseSourceLocator } from "@/lib/dataroom-utils";
import { getFormulaAssignmentSpec } from "@/lib/formula-input-assignment";
import {
  DefinedItem,
  ExtractedItem,
  MappingRow,
  SourceFile,
} from "@/lib/types";

export interface DefinitionEngineInput {
  sourceFiles: SourceFile[];
  extractedItems: ExtractedItem[];
  mappingRows: MappingRow[];
}

export interface DefinitionEngine {
  name: string;
  defineItems: (input: DefinitionEngineInput) => DefinedItem[];
}

const definitionTemplates: Record<
  string,
  {
    definition: string;
    detectedType: string;
    defaultCalculationType: DefinedItem["calculationType"];
    defaultDependencies: string[];
  }
> = {
  Revenue: {
    definition: "Top-line revenue recognized in the source statement for the stated period.",
    detectedType: "P&L Line Item",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  COGS: {
    definition: "Direct costs required to deliver the reported revenue base.",
    detectedType: "P&L Line Item",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  "Gross Profit": {
    definition: "Revenue less cost of goods sold for the stated period.",
    detectedType: "Derived P&L Line",
    defaultCalculationType: "Formula",
    defaultDependencies: ["Revenue", "COGS"],
  },
  "Operating Expenses": {
    definition: "Operating costs below gross profit and outside direct delivery cost.",
    detectedType: "P&L Line Item",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  EBITDA: {
    definition: "Earnings before interest, taxes, depreciation, and amortization.",
    detectedType: "Derived P&L Line",
    defaultCalculationType: "Formula",
    defaultDependencies: ["Gross Profit", "Operating Expenses"],
  },
  ARR: {
    definition: "Annual recurring revenue attributable to active recurring contracts.",
    detectedType: "Recurring Revenue KPI",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  "Net Revenue Retention": {
    definition: "Net revenue retention percentage reported for the stated period.",
    detectedType: "Retention KPI",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  "Customer Churn": {
    definition: "Customer or revenue churn percentage reported for the stated period.",
    detectedType: "Customer KPI",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  Headcount: {
    definition: "Reported employee or FTE count for the stated period.",
    detectedType: "Operating KPI",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  CapEx: {
    definition: "Capital expenditure invested during the stated period.",
    detectedType: "Cash Flow Line",
    defaultCalculationType: "Source Reported",
    defaultDependencies: [],
  },
  Unmapped: {
    definition: "The item could not be placed into the standard databook taxonomy yet.",
    detectedType: "Unclassified Line Item",
    defaultCalculationType: "Manual Review",
    defaultDependencies: [],
  },
};

function inferDerivedSourceMetric(rawLabel: string, mappedCategory: string) {
  const normalized = normalizeLabel(`${mappedCategory} ${rawLabel}`);

  if (normalized.includes("margin")) {
    return {
      directOrDerived: "Derived" as const,
      calculationType: "Ratio" as const,
    };
  }

  if (
    mappedCategory === "Gross Profit" ||
    mappedCategory === "EBITDA" ||
    normalized.includes("adjusted ebitda")
  ) {
    return {
      directOrDerived: "Derived" as const,
      calculationType: "Formula" as const,
    };
  }

  return {
    directOrDerived: "Direct" as const,
    calculationType:
      definitionTemplates[mappedCategory]?.defaultCalculationType ?? ("Source Reported" as const),
  };
}

function buildExtractedRowIndex(extractedItems: ExtractedItem[]) {
  const rowIndex = new Map<
    string,
    {
      period: string;
      sourceSheetName: string;
    }
  >();

  for (const item of extractedItems) {
    for (const row of item.rows ?? []) {
      rowIndex.set(`${item.sourceFileId}:${row.location}`, {
        period: item.period,
        sourceSheetName: item.tableName ?? item.title,
      });
    }
  }

  return rowIndex;
}

function buildDefinitionRationale(params: {
  rawLabel: string;
  mappedCategory: string;
  reasoning: string;
  sourceFileName: string;
  traceabilityStatus: DefinedItem["traceabilityStatus"];
  formulaDependencies: string[];
  entersCorePipeline?: boolean;
  routingBucket?: DefinedItem["routingBucket"];
}) {
  if (params.entersCorePipeline === false) {
    return `The row was routed into ${params.routingBucket?.toLowerCase() ?? "supporting detail"} instead of the core databook flow. ${params.reasoning} Source trace is preserved for reference, but the row does not currently drive workbook formulas.`.trim();
  }

  const categoryText =
    params.mappedCategory === "Unmapped"
      ? "No standard databook category could be assigned deterministically."
      : `"${params.rawLabel}" was interpreted as ${params.mappedCategory}.`;
  const dependencyText =
    params.formulaDependencies.length > 0
      ? ` This line behaves like a formula-sensitive metric and may depend on ${params.formulaDependencies.join(", ")}.`
      : "";
  const traceText =
    params.traceabilityStatus === "Traced"
      ? ` Source trace is preserved from ${params.sourceFileName}.`
      : " Source trace is incomplete and should be reviewed before relying on the value.";

  return `${categoryText} ${params.reasoning}${dependencyText}${traceText}`.trim();
}

export const deterministicDefinitionEngine: DefinitionEngine = {
  name: "deterministic-v1",
  defineItems({ sourceFiles, extractedItems, mappingRows }) {
    const sourceFileIndex = new Map(sourceFiles.map((file) => [file.id, file]));
    const extractedRowIndex = buildExtractedRowIndex(extractedItems);

    return mappingRows.map((row) => {
      const sourceFile = sourceFileIndex.get(row.sourceFileId);
      const extractedRow = extractedRowIndex.get(`${row.sourceFileId}:${row.sourceLocator}`);
      const sourceInfo = parseSourceLocator(row.sourceLocator);
      const mappedCategory = row.mappedCategory || "Unmapped";
      const template = definitionTemplates[mappedCategory] ?? definitionTemplates.Unmapped;
      const formulaAssignment = getFormulaAssignmentSpec(mappedCategory);
      const unit = detectUnit({
        rawValue: row.rawValue,
        rawLabel: row.rawLineItemLabel,
        mappedCategory,
      });
      const normalizedValue = normalizeValueForUnit(row.rawValue, unit);
      const classification = row.directOrDerivedHint
        ? {
            directOrDerived: row.directOrDerivedHint,
            calculationType:
              row.directOrDerivedHint === "Derived"
                ? ("Formula" as const)
                : template.defaultCalculationType,
          }
        : inferDerivedSourceMetric(row.rawLineItemLabel, mappedCategory);
      const formulaDependencies =
        classification.directOrDerived === "Derived"
          ? dedupeStrings(
              row.dependencyCandidatesHint && row.dependencyCandidatesHint.length > 0
                ? row.dependencyCandidatesHint
                : template.defaultDependencies.length > 0
                  ? template.defaultDependencies
                  : formulaAssignment.dependencyCandidates,
            )
          : [];
      const traceabilityStatus =
        sourceFile && sourceInfo.sourceLocation && sourceInfo.sourceSheetName
          ? "Traced"
          : sourceFile
            ? "Partial"
            : "Missing";
      const entersCorePipeline = row.entersCorePipeline !== false;
      const fallbackDefinition =
        entersCorePipeline
          ? template.definition
          : `${row.routingBucket ?? "Supporting Detail"} excluded from the core databook flow unless a human explicitly promotes it.`;

      return {
        id: `defined-${row.id}`,
        sourceFileId: row.sourceFileId,
        sourceFileName: sourceFile?.name ?? "Unknown file",
        sourceSheetName: extractedRow?.sourceSheetName ?? sourceInfo.sourceSheetName,
        sourceLocation: row.sourceLocator || "Not captured",
        period: row.period || extractedRow?.period || "Current Period",
        rawLabel: row.rawLineItemLabel,
        rawValue: row.rawValue,
        normalizedValue,
        unit,
        detectedType: template.detectedType,
        mappedCategory,
        mappedMetric: mappedCategory,
        outputLineKey: formulaAssignment.outputLineKey,
        formulaRole: formulaAssignment.formulaRole,
        dependencyCandidates: formulaAssignment.dependencyCandidates,
        formulaTemplateKey: formulaAssignment.formulaTemplateKey,
        definition: row.definitionHint?.trim() || fallbackDefinition,
        rationale: buildDefinitionRationale({
          rawLabel: row.rawLineItemLabel,
          mappedCategory,
          reasoning: row.reasoning,
          sourceFileName: sourceFile?.name ?? "Unknown file",
          traceabilityStatus,
          formulaDependencies,
          entersCorePipeline,
          routingBucket: row.routingBucket,
        }),
        calculationType: classification.calculationType,
        directOrDerived: classification.directOrDerived,
        formulaDependencies,
        reviewStatus:
          !entersCorePipeline
            ? "Rule Applied"
            : row.status === "Needs Review" || row.status === "Pending" || mappedCategory === "Unmapped"
            ? "Flagged"
            : row.status,
        traceabilityStatus,
        routingBucket: row.routingBucket,
        entersCorePipeline,
        routingReason: row.routingReason,
      } satisfies DefinedItem;
    });
  },
};

export function buildDefinedItems(input: DefinitionEngineInput) {
  return deterministicDefinitionEngine.defineItems(input);
}
