import { getFileName } from "@/lib/mock-data";
import { Deal, ExceptionItem, ExtractedItem, MappingRow } from "@/lib/types";

export interface CopilotCitation {
  fileId: string;
  fileName: string;
  locator: string;
  label: string;
  note: string;
}

export interface CopilotReply {
  text: string;
  citations: CopilotCitation[];
}

const stopWords = new Set([
  "a",
  "an",
  "and",
  "are",
  "be",
  "by",
  "for",
  "from",
  "how",
  "i",
  "in",
  "is",
  "it",
  "me",
  "of",
  "or",
  "show",
  "should",
  "the",
  "this",
  "to",
  "us",
  "what",
  "where",
  "which",
  "why",
  "with",
]);

function tokenize(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9\u3400-\u9fff%$]+/g, " ")
    .split(/\s+/)
    .filter((token) => token && !stopWords.has(token));
}

function hasKeyword(query: string, keywords: string[]) {
  return keywords.some((keyword) => query.includes(keyword));
}

function scoreFields(query: string, fields: string[]) {
  const tokens = tokenize(query);
  const haystack = fields.join(" ").toLowerCase();

  return tokens.reduce((score, token) => {
    if (haystack.includes(token)) {
      return score + 1;
    }

    return score;
  }, 0);
}

function buildMappingCitation(deal: Deal, row: MappingRow): CopilotCitation {
  return {
    fileId: row.sourceFileId,
    fileName: getFileName(deal, row.sourceFileId),
    locator: row.sourceLocator,
    label: `${row.mappedCategory} • ${row.period}`,
    note: row.rawLineItemLabel,
  };
}

function buildExtractedCitation(deal: Deal, item: ExtractedItem): CopilotCitation {
  return {
    fileId: item.sourceFileId,
    fileName: getFileName(deal, item.sourceFileId),
    locator: item.detectedTableType,
    label: `${item.title} • ${item.period}`,
    note: item.summary,
  };
}

function buildExceptionCitation(deal: Deal, item: ExceptionItem): CopilotCitation | null {
  const matchingRow =
    deal.mappingRows.find((row) =>
      item.affectedLineItem.toLowerCase().includes(row.mappedCategory.toLowerCase()),
    ) ??
    deal.mappingRows.find((row) =>
      item.affectedLineItem.toLowerCase().includes(row.rawLineItemLabel.toLowerCase()),
    );

  return matchingRow ? buildMappingCitation(deal, matchingRow) : null;
}

function dedupeCitations(citations: CopilotCitation[]) {
  const seen = new Set<string>();

  return citations.filter((citation) => {
    const key = `${citation.fileId}:${citation.locator}:${citation.label}`;

    if (seen.has(key)) {
      return false;
    }

    seen.add(key);
    return true;
  });
}

function rankMappingRows(deal: Deal, query: string) {
  return [...deal.mappingRows]
    .map((row) => {
      let score = scoreFields(query, [
        row.mappedCategory,
        row.rawLineItemLabel,
        row.reasoning,
        row.sourceLocator,
        row.period,
        getFileName(deal, row.sourceFileId),
      ]);

      if (query.includes(row.mappedCategory.toLowerCase())) {
        score += 4;
      }

      if (query.includes(row.rawLineItemLabel.toLowerCase())) {
        score += 4;
      }

      return { row, score };
    })
    .sort((a, b) => b.score - a.score);
}

function rankExtractedItems(deal: Deal, query: string) {
  return [...deal.extractedItems]
    .map((item) => ({
      item,
      score: scoreFields(query, [
        item.title,
        item.detectedTableType,
        item.summary,
        item.period,
        getFileName(deal, item.sourceFileId),
      ]),
    }))
    .sort((a, b) => b.score - a.score);
}

function rankExceptions(deal: Deal, query: string) {
  return [...deal.exceptions]
    .map((item) => ({
      item,
      score: scoreFields(query, [
        item.affectedLineItem,
        item.category,
        item.detail,
        item.suggestedResolution,
        item.severity,
      ]),
    }))
    .sort((a, b) => b.score - a.score);
}

function getPreferredMappingRow(deal: Deal, query: string) {
  const [match] = rankMappingRows(deal, query);

  if (match && match.score > 0) {
    return match.row;
  }

  return (
    deal.mappingRows.find((row) => row.status === "Needs Review" || row.status === "Pending") ??
    deal.mappingRows.find((row) => row.status === "Rule Applied") ??
    deal.mappingRows[0]
  );
}

function getPreferredExtractedItem(deal: Deal, query: string) {
  const [match] = rankExtractedItems(deal, query);

  if (match && match.score > 0) {
    return match.item;
  }

  return (
    deal.extractedItems.find((item) =>
      item.issueFlags.some((flag) => flag.toLowerCase() !== "none"),
    ) ?? deal.extractedItems[0]
  );
}

export function getCopilotReply(deal: Deal, prompt: string): CopilotReply {
  const query = prompt.trim();
  const normalized = query.toLowerCase();

  if (!query) {
    return {
      text: "Ask about mapping rationale, review blockers, or where a value came from in the source files.",
      citations: [],
    };
  }

  const primaryRow = getPreferredMappingRow(deal, normalized);
  const primaryExtractedItem = getPreferredExtractedItem(deal, normalized);
  const openExceptions = deal.exceptions.filter((item) => item.status === "Open");
  const blockingExceptions = openExceptions.filter(
    (item) => item.severity === "Critical" || item.severity === "High",
  );

  if (
    hasKeyword(normalized, [
      "why",
      "mapped",
      "mapping",
      "map this",
      "explain",
      "为什么",
      "怎么映射",
      "映射",
    ])
  ) {
    return {
      text: `${primaryRow.mappedCategory} is currently tied to "${primaryRow.rawLineItemLabel}" from ${getFileName(deal, primaryRow.sourceFileId)} at ${primaryRow.sourceLocator}. The workflow rationale is: ${primaryRow.reasoning}${primaryRow.status === "Needs Review" || primaryRow.status === "Pending" ? ` This row still needs analyst review before it can flow cleanly into outputs.` : ""}`,
      citations: dedupeCitations([buildMappingCitation(deal, primaryRow)]),
    };
  }

  if (
    hasKeyword(normalized, [
      "where",
      "source",
      "trace",
      "come from",
      "which file",
      "data from",
      "哪里",
      "来源",
      "哪个文件",
      "file",
    ])
  ) {
    return {
      text: `The current source support points to ${getFileName(deal, primaryRow.sourceFileId)} at ${primaryRow.sourceLocator}. That file carries "${primaryRow.rawLineItemLabel}" with a value of ${primaryRow.rawValue} for ${primaryRow.period}, which is what currently supports the ${primaryRow.mappedCategory} mapping.`,
      citations: dedupeCitations([
        buildMappingCitation(deal, primaryRow),
        buildExtractedCitation(deal, primaryExtractedItem),
      ]),
    };
  }

  if (
    hasKeyword(normalized, [
      "compare",
      "vs",
      "versus",
      "mapped vs source",
      "difference",
      "对比",
      "比较",
    ])
  ) {
    return {
      text: `Mapped output and source are currently aligned through one row: ${primaryRow.mappedCategory} is populated from "${primaryRow.rawLineItemLabel}" in ${getFileName(deal, primaryRow.sourceFileId)} at ${primaryRow.sourceLocator}. The source value is ${primaryRow.rawValue}, confidence is ${primaryRow.confidence}%, and the row status is ${primaryRow.status}.`,
      citations: dedupeCitations([buildMappingCitation(deal, primaryRow)]),
    };
  }

  if (
    hasKeyword(normalized, [
      "exception",
      "exceptions",
      "review",
      "blocker",
      "blocked",
      "issue",
      "queue",
      "异常",
      "阻塞",
      "审核",
    ])
  ) {
    const highlightedExceptions = rankExceptions(deal, normalized)
      .map((match) => match.item)
      .filter((item) => item.status === "Open");
    const focusItems = (highlightedExceptions.length > 0 ? highlightedExceptions : openExceptions).slice(
      0,
      2,
    );

    return {
      text:
        blockingExceptions.length > 0
          ? `There are ${openExceptions.length} open review items, and ${blockingExceptions.length} of them are currently blocking outputs. The highest-priority items are ${focusItems
              .map((item) => item.affectedLineItem)
              .join(" and ")}.`
          : `There are ${openExceptions.length} open review items. The queue is no longer blocked by critical severity, but ${focusItems
              .map((item) => item.affectedLineItem)
              .join(" and ")} still need explicit resolution before outputs should be finalized.`,
      citations: dedupeCitations(
        focusItems
          .map((item) => buildExceptionCitation(deal, item))
          .filter((citation): citation is CopilotCitation => Boolean(citation)),
      ),
    };
  }

  if (
    hasKeyword(normalized, [
      "ic",
      "investment committee",
      "prep notes",
      "notes",
      "memo",
      "委员会",
    ])
  ) {
    const revenueRow =
      deal.mappingRows.find((row) => row.mappedCategory === "Revenue") ?? primaryRow;
    const ebitdaRow =
      deal.mappingRows.find((row) => row.mappedCategory === "EBITDA") ?? primaryRow;

    return {
      text: `${deal.targetCompanyName} is currently staged at ${deal.status} with TTM revenue of ${revenueRow.rawValue} and EBITDA of ${ebitdaRow.rawValue}. Before IC materials are circulated, the team still needs to resolve ${openExceptions.length} review items${blockingExceptions.length > 0 ? `, including ${blockingExceptions.length} blocking exceptions` : ""}.`,
      citations: dedupeCitations([
        buildMappingCitation(deal, revenueRow),
        buildMappingCitation(deal, ebitdaRow),
        ...blockingExceptions
          .slice(0, 1)
          .map((item) => buildExceptionCitation(deal, item))
          .filter((citation): citation is CopilotCitation => Boolean(citation)),
      ]),
    };
  }

  if (
    hasKeyword(normalized, [
      "extract",
      "extraction",
      "scan",
      "table",
      "staging",
      "validate",
      "validation",
      "提取",
      "表",
    ])
  ) {
    const flaggedTables = deal.extractedItems.filter((item) =>
      item.issueFlags.some((flag) => flag.toLowerCase() !== "none"),
    );
    const focusItem = primaryExtractedItem;

    return {
      text:
        flaggedTables.length > 0
          ? `${deal.extractedItems.length} extracted tables are staged, and ${flaggedTables.length} still carry quality flags. The most relevant table here is ${focusItem.title} from ${getFileName(deal, focusItem.sourceFileId)}, which is currently marked for ${focusItem.issueFlags.join(", ").toLowerCase()}.`
          : `${deal.extractedItems.length} extracted tables are staged and currently look clean enough to move through mapping. The closest match to your question is ${focusItem.title} from ${getFileName(deal, focusItem.sourceFileId)}.`,
      citations: dedupeCitations([buildExtractedCitation(deal, focusItem)]),
    };
  }

  return {
    text: `I can help trace a value back to source, explain a mapping decision, summarize the review queue, or draft short IC prep notes. Right now the most operational next step is to clear ${blockingExceptions.length > 0 ? `${blockingExceptions.length} blocking review items` : "the remaining flagged mappings"} before outputs are finalized.`,
    citations: dedupeCitations([buildMappingCitation(deal, primaryRow)]),
  };
}
