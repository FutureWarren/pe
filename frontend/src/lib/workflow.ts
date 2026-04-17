import { Deal, ExceptionItem, MappingRow, OutputAsset } from "@/lib/types";
import { isActionableReviewItem, isBlockingCoreIssue } from "@/lib/review-utils";

export type WorkflowStageKey = "intake" | "extraction" | "mapping" | "review" | "outputs";

export type WorkflowStageStatus =
  | "Complete"
  | "In Progress"
  | "Blocked"
  | "Not Started"
  | "Ready";

export interface WorkflowStage {
  key: WorkflowStageKey;
  label: string;
  step: number;
  status: WorkflowStageStatus;
  detail: string;
  href?: string;
}

export interface WorkflowSnapshot {
  stages: WorkflowStage[];
  currentActionStage: WorkflowStageKey;
  openExceptions: number;
  blockingExceptions: number;
  extractionItemsWithIssues: number;
  approvedMappings: number;
  unresolvedMappings: number;
  readyOutputs: number;
}

interface WorkflowOverrides {
  exceptions?: ExceptionItem[];
  mappingRows?: MappingRow[];
  outputs?: OutputAsset[];
}

function pluralize(count: number, singular: string, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function getWorkflowSnapshot(
  deal: Deal,
  overrides: WorkflowOverrides = {},
): WorkflowSnapshot {
  const exceptions = overrides.exceptions ?? deal.exceptions;
  const mappingRows = overrides.mappingRows ?? deal.mappingRows;
  const outputs = overrides.outputs ?? deal.outputs;
  const coreMappingRows = mappingRows.filter((row) => row.entersCorePipeline !== false);
  const coreExceptions = exceptions.filter((item) => (item.scope ?? "core") === "core");
  const actionableCoreExceptions = coreExceptions.filter((item) => isActionableReviewItem(item));

  const extractionItemsWithIssues = deal.extractedItems.filter((item) =>
    (item.coreRowCount ?? 0) > 0 &&
    item.issueFlags.some((flag) => flag.toLowerCase() !== "none"),
  ).length;
  const approvedMappings = coreMappingRows.filter(
    (row) => row.status === "Approved" || row.status === "Rule Applied",
  ).length;
  const unresolvedMappings = coreMappingRows.filter(
    (row) => row.status === "Pending" || row.status === "Needs Review",
  ).length;
  const openExceptions = actionableCoreExceptions.filter((item) => item.status === "Open").length;
  const blockingExceptions = actionableCoreExceptions.filter(
    (item) => item.status === "Open" && isBlockingCoreIssue(item),
  ).length;
  const readyOutputs = outputs.filter((output) => output.status === "Ready").length;

  const intakeStatus: WorkflowStageStatus =
    deal.sourceFiles.length > 0 ? "Complete" : "Not Started";
  const extractionStatus: WorkflowStageStatus =
    deal.extractedItems.length === 0
      ? deal.sourceFiles.length > 0
        ? "In Progress"
        : "Not Started"
      : deal.extractionProgress >= 80
        ? "Complete"
        : "In Progress";
  const mappingStatus: WorkflowStageStatus =
    deal.extractedItems.length === 0
      ? "Not Started"
      : coreMappingRows.length === 0
        ? "Not Started"
        : unresolvedMappings === 0
          ? "Complete"
          : "In Progress";
  const reviewStatus: WorkflowStageStatus =
    coreMappingRows.length === 0
      ? "Not Started"
      : blockingExceptions > 0
        ? "Blocked"
        : openExceptions > 0
          ? "In Progress"
          : exceptions.length > 0
            ? "Complete"
            : "Ready";
  const outputsStatus: WorkflowStageStatus =
    readyOutputs > 0 && blockingExceptions === 0 && unresolvedMappings === 0
      ? "Ready"
      : blockingExceptions > 0 || unresolvedMappings > 0
        ? "Blocked"
        : outputs.length > 0
          ? "In Progress"
          : "Not Started";

  const stages: WorkflowStage[] = [
    {
      key: "intake",
      label: "Intake",
      step: 1,
      status: intakeStatus,
      detail:
        deal.sourceFiles.length > 0
          ? `${deal.sourceFiles.length} files connected`
          : "Awaiting files",
      href: `/deals/${deal.id}/intake`,
    },
    {
      key: "extraction",
      label: "Extraction",
      step: 2,
      status: extractionStatus,
      detail:
        extractionStatus === "Complete"
          ? `${pluralize(deal.extractedItems.length, "table")} staged`
          : `${deal.extractionProgress}% scanned`,
      href: `/deals/${deal.id}/extraction`,
    },
    {
      key: "mapping",
      label: "Mapping",
      step: 3,
      status: mappingStatus,
      detail:
        mappingRows.length === 0
          ? "Awaiting extracted rows"
          : unresolvedMappings === 0
            ? `${pluralize(approvedMappings, "row")} approved`
            : `${pluralize(unresolvedMappings, "row")} unresolved`,
      href: `/deals/${deal.id}/mapping`,
    },
    {
      key: "review",
      label: "Review",
      step: 4,
      status: reviewStatus,
      detail:
        blockingExceptions > 0
          ? `${pluralize(blockingExceptions, "blocking item")} open`
          : openExceptions > 0
            ? `${pluralize(openExceptions, "open item")} remaining`
            : "Queue clear",
      href: `/deals/${deal.id}/review`,
    },
    {
      key: "outputs",
      label: "Outputs",
      step: 5,
      status: outputsStatus,
      detail:
        outputsStatus === "Ready"
          ? `${pluralize(readyOutputs, "output")} ready`
          : outputsStatus === "Blocked"
            ? blockingExceptions > 0
              ? `${pluralize(blockingExceptions, "review blocker")}`
              : `${pluralize(unresolvedMappings, "mapping blocker")}`
            : `${pluralize(outputs.length, "package")} staged`,
      href: `/deals/${deal.id}/outputs`,
    },
  ];

  const currentActionStage =
    stages.find((stage) => stage.status === "Blocked")?.key ??
    stages.find((stage) => stage.status === "In Progress")?.key ??
    stages.find((stage) => stage.status === "Not Started")?.key ??
    "outputs";

  return {
    stages,
    currentActionStage,
    openExceptions,
    blockingExceptions,
    extractionItemsWithIssues,
    approvedMappings,
    unresolvedMappings,
    readyOutputs,
  };
}

export function getCurrentPageStage(pathname: string): WorkflowStageKey | null {
  if (pathname.endsWith("/intake")) return "intake";
  if (pathname.endsWith("/extraction")) return "extraction";
  if (pathname.endsWith("/mapping")) return "mapping";
  if (pathname.endsWith("/review")) return "review";
  if (pathname.includes("/outputs")) return "outputs";
  return null;
}

export function getSourceFileWorkflowStatus(
  deal: Deal,
  sourceFileId: string,
): { label: string; tone: "success" | "warning" | "danger" | "muted" | "accent" } {
  const file = deal.sourceFiles.find((item) => item.id === sourceFileId);
  const extractedItems = deal.extractedItems.filter((item) => item.sourceFileId === sourceFileId);
  const mappedRows = deal.mappingRows.filter((row) => row.sourceFileId === sourceFileId);
  const hasFlaggedExtraction = extractedItems.some((item) =>
    item.issueFlags.some((flag) => flag.toLowerCase() !== "none"),
  );
  const hasReviewRows = mappedRows.some((row) => row.status === "Needs Review");
  const hasPendingRows = mappedRows.some((row) => row.status === "Pending");
  const allMapped =
    mappedRows.length > 0 &&
    mappedRows.every((row) => row.status === "Approved" || row.status === "Rule Applied");

  if (file?.status === "Flagged") {
    return { label: "Issue flagged", tone: "danger" };
  }

  if (hasReviewRows) {
    return { label: "Pending review", tone: "warning" };
  }

  if (hasPendingRows) {
    return { label: "Partial", tone: "accent" };
  }

  if (allMapped) {
    return { label: "Mapped", tone: "success" };
  }

  if (hasFlaggedExtraction) {
    return { label: "Issue flagged", tone: "warning" };
  }

  if (extractedItems.length > 0) {
    return { label: "Extracted", tone: "accent" };
  }

  if (mappedRows.length === 0 && extractedItems.length === 0) {
    return { label: "Unused", tone: "muted" };
  }

  return { label: "Partial", tone: "muted" };
}

export function isSourceFileRelevantToPage(
  deal: Deal,
  sourceFileId: string,
  pathname: string,
) {
  if (pathname.endsWith("/intake")) {
    return deal.sourceFiles.some((file) => file.id === sourceFileId);
  }

  if (pathname.endsWith("/extraction")) {
    return deal.extractedItems.some((item) => item.sourceFileId === sourceFileId);
  }

  if (pathname.endsWith("/mapping") || pathname.endsWith("/review") || pathname.includes("/outputs")) {
    return deal.mappingRows.some((row) => row.sourceFileId === sourceFileId);
  }

  return false;
}
