import { ExceptionItem, ReviewIssueClass } from "@/lib/types";

export function getReviewIssueClass(item: ExceptionItem): ReviewIssueClass {
  if (item.issueClass) {
    return item.issueClass;
  }

  if ((item.issueLevel ?? "row") === "table") {
    return "table_warning";
  }

  if (item.blocksExport ?? (item.severity === "High" || item.severity === "Critical")) {
    return "real_core_blocker";
  }

  return "non_blocking_mapping_bug";
}

export function isBlockingCoreIssue(item: ExceptionItem) {
  return getReviewIssueClass(item) === "real_core_blocker";
}

export function isNonBlockingRowIssue(item: ExceptionItem) {
  return (
    getReviewIssueClass(item) === "non_blocking_mapping_bug" ||
    getReviewIssueClass(item) === "kpi_scope_issue"
  );
}

export function isTableWarning(item: ExceptionItem) {
  return getReviewIssueClass(item) === "table_warning";
}

export function isActionableReviewItem(item: ExceptionItem) {
  const issueClass = getReviewIssueClass(item);

  return (
    issueClass === "real_core_blocker" ||
    issueClass === "non_blocking_mapping_bug" ||
    issueClass === "kpi_scope_issue"
  );
}
