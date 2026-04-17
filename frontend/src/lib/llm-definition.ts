import { parseSourceLocator } from "@/lib/dataroom-utils";
import { standardTags } from "@/lib/mock-data";
import type { IntakeScanResult } from "@/lib/local-pipeline";
import type { ExtractedItem, MappingRow, MappingStatus } from "@/lib/types";

const unmappedCategory = "Unmapped";
const mappingTagOptions = [unmappedCategory, ...standardTags];

export interface DefinitionRequestRow {
  mappingRowId: string;
  sourceFileId: string;
  sourceFileName: string;
  sourceSheetName: string;
  sourceLocation: string;
  period: string;
  rawLabel: string;
  rawValue: string;
  currentMappedCategory: string;
  currentConfidence: number;
  currentStatus: MappingStatus;
  currentReasoning: string;
}

export interface GeminiDefinitionResult {
  mappingRowId: string;
  mappedCategory: string;
  reviewStatus: MappingStatus;
  confidence: number;
  definition: string;
  rationale: string;
  directOrDerivedHint: "Direct" | "Derived";
  dependencyCandidates: string[];
}

interface GeminiDefinitionResponse {
  enabled: boolean;
  provider: "gemini" | "deterministic";
  model?: string;
  results: GeminiDefinitionResult[];
  fallbackReason?: string;
}

function buildExtractedRowIndex(extractedItems: ExtractedItem[]) {
  const rowIndex = new Map<
    string,
    {
      sourceSheetName: string;
    }
  >();

  for (const item of extractedItems) {
    for (const row of item.rows ?? []) {
      rowIndex.set(`${item.sourceFileId}:${row.location}`, {
        sourceSheetName: item.tableName ?? item.title,
      });
    }
  }

  return rowIndex;
}

export function buildDefinitionRequestRows(scanResult: IntakeScanResult): DefinitionRequestRow[] {
  const sourceFileIndex = new Map(
    scanResult.sourceFiles.map((file) => [file.id, file]),
  );
  const extractedRowIndex = buildExtractedRowIndex(scanResult.extractedItems);

  return scanResult.mappingRows
    .filter((row) => row.entersCorePipeline !== false)
    .map((row) => {
    const sourceFile = sourceFileIndex.get(row.sourceFileId);
    const extractedRow = extractedRowIndex.get(`${row.sourceFileId}:${row.sourceLocator}`);
    const locator = parseSourceLocator(row.sourceLocator);

    return {
      mappingRowId: row.id,
      sourceFileId: row.sourceFileId,
      sourceFileName: sourceFile?.name ?? "Unknown file",
      sourceSheetName:
        extractedRow?.sourceSheetName ?? locator.sourceSheetName ?? "Imported file",
      sourceLocation: row.sourceLocator,
      period: row.period,
      rawLabel: row.rawLineItemLabel,
      rawValue: row.rawValue,
      currentMappedCategory: row.mappedCategory,
      currentConfidence: row.confidence,
      currentStatus: row.status,
      currentReasoning: row.reasoning,
    };
    });
}

function sanitizeMappedCategory(value: string) {
  return mappingTagOptions.includes(value) ? value : unmappedCategory;
}

function sanitizeReviewStatus(value: string, mappedCategory: string): MappingStatus {
  if (mappedCategory === unmappedCategory) {
    return "Needs Review";
  }

  if (value === "Approved" || value === "Pending" || value === "Needs Review" || value === "Rule Applied") {
    return value;
  }

  return "Pending";
}

function sanitizeConfidence(value: number) {
  if (!Number.isFinite(value)) {
    return 50;
  }

  return Math.max(0, Math.min(100, Math.round(value)));
}

function sanitizeDependencyCandidates(value: string[]) {
  return value
    .filter(Boolean)
    .slice(0, 6)
    .map((candidate) => candidate.trim());
}

export function applyGeminiInterpretationsToMappingRows(
  mappingRows: MappingRow[],
  results: GeminiDefinitionResult[],
) {
  const resultIndex = new Map(results.map((result) => [result.mappingRowId, result]));

  return mappingRows.map((row) => {
    const result = resultIndex.get(row.id);

    if (!result) {
      return row;
    }

    const mappedCategory = sanitizeMappedCategory(result.mappedCategory);
    const reviewStatus = sanitizeReviewStatus(result.reviewStatus, mappedCategory);

    return {
      ...row,
      mappedCategory,
      confidence: sanitizeConfidence(result.confidence),
      status: reviewStatus,
      reasoning: result.rationale?.trim() || row.reasoning,
      definitionHint: result.definition?.trim() || row.definitionHint,
      directOrDerivedHint:
        result.directOrDerivedHint === "Derived" ? ("Derived" as const) : ("Direct" as const),
      dependencyCandidatesHint: sanitizeDependencyCandidates(result.dependencyCandidates ?? []),
      interpretationProvider: "gemini" as const,
    };
  });
}

export async function enhanceScanResultWithGemini(
  scanResult: IntakeScanResult,
): Promise<IntakeScanResult> {
  const requestRows = buildDefinitionRequestRows(scanResult);

  if (requestRows.length === 0) {
    return scanResult;
  }

  try {
    const response = await fetch("/api/definition-engine", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        rows: requestRows,
      }),
    });

    if (!response.ok) {
      return scanResult;
    }

    const payload = (await response.json()) as GeminiDefinitionResponse;

    if (!payload.enabled || payload.provider !== "gemini" || payload.results.length === 0) {
      return scanResult;
    }

    return {
      ...scanResult,
      mappingRows: applyGeminiInterpretationsToMappingRows(
        scanResult.mappingRows,
        payload.results,
      ),
    };
  } catch {
    return scanResult;
  }
}
