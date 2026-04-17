import { Deal, OutputAsset } from "@/lib/types";
import { getBackendWorkbookDownloadPath } from "@/lib/backend-pipeline";
import { buildDatabookExportRows } from "@/lib/local-pipeline";
import { downloadDatabookWorkbook } from "@/lib/workbook-export";

function escapeCsv(value: string) {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replaceAll('"', '""')}"`;
  }

  return value;
}

export function serializeOutputToCsv(deal: Deal, output: OutputAsset) {
  const databookRows = buildDatabookExportRows(deal);
  const headerLines = [
    ["Deal", deal.targetCompanyName],
    ["Output", output.name],
    ["Generated Date", output.generatedDate],
    ["Review Status", output.reviewStatus],
    [],
  ];

  const headerCsv = headerLines.map((line) => line.map(escapeCsv).join(",")).join("\n");

  if (databookRows.length > 0) {
    const bodyLines = [
      [
        "source_file",
        "source_location",
        "raw_label",
        "raw_value",
        "mapped_category",
        "direct_or_derived",
        "confidence",
        "status",
        "definition",
        "reasoning",
        "traceability_status",
      ],
      ...databookRows.map((row) => [
        row.source_file,
        row.source_location,
        row.raw_label,
        row.raw_value,
        row.mapped_category,
        row.direct_or_derived,
        row.confidence,
        row.status,
        row.definition,
        row.reasoning,
        row.traceability_status,
      ]),
    ];

    return `${headerCsv}\n${bodyLines.map((line) => line.map(escapeCsv).join(",")).join("\n")}`;
  }

  if (output.previewType === "table" && output.previewRows) {
    const bodyLines = [
      ["Line Item", "Primary Value", "Secondary Value", "Trace"],
      ...output.previewRows.map((row) => [
        row.item,
        row.valueA,
        row.valueB ?? "",
        row.trace,
      ]),
    ];

    return `${headerCsv}\n${bodyLines.map((line) => line.map(escapeCsv).join(",")).join("\n")}`;
  }

  const sectionLines = [
    ["Section", "Detail"],
    ...(output.previewSections ?? []).flatMap((section) =>
      section.bullets.map((bullet) => [section.heading, bullet]),
    ),
  ];

  return `${headerCsv}\n${sectionLines.map((line) => line.map(escapeCsv).join(",")).join("\n")}`;
}

export function serializeDatabookCsv(deal: Deal) {
  const databookRows = buildDatabookExportRows(deal);
  const headerLines = [
    ["Product", "Angelic Dataroom"],
    ["Import", deal.targetCompanyName],
    ["Generated Date", new Date().toISOString()],
    ["Status", deal.outputsReady ? "Ready" : "Needs Review"],
    [],
  ];
  const headerCsv = headerLines.map((line) => line.map(escapeCsv).join(",")).join("\n");
  const bodyLines = [
    [
      "source_file",
      "source_location",
      "raw_label",
      "raw_value",
      "mapped_category",
      "direct_or_derived",
      "confidence",
      "status",
      "definition",
      "reasoning",
      "traceability_status",
    ],
    ...databookRows.map((row) => [
      row.source_file,
      row.source_location,
      row.raw_label,
      row.raw_value,
      row.mapped_category,
      row.direct_or_derived,
      row.confidence,
      row.status,
      row.definition,
      row.reasoning,
      row.traceability_status,
    ]),
  ];

  return `${headerCsv}\n${bodyLines.map((line) => line.map(escapeCsv).join(",")).join("\n")}`;
}

export function downloadDatabookCsv(deal: Deal) {
  const csv = serializeDatabookCsv(deal);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const fileStub = `${deal.targetCompanyName}-databook`
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-|-$/g, "");

  link.href = url;
  link.download = `${fileStub}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function downloadDatabookXlsx(deal: Deal) {
  const backendWorkbookPath = getBackendWorkbookDownloadPath(deal);

  if (backendWorkbookPath) {
    const link = document.createElement("a");
    link.href = backendWorkbookPath;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    return;
  }

  downloadDatabookWorkbook(deal);
}

export function downloadOutputCsv(deal: Deal, output: OutputAsset) {
  const csv = serializeOutputToCsv(deal, output);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const fileStub = `${deal.targetCompanyName}-${output.name}`
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "-")
    .replaceAll(/^-|-$/g, "");

  link.href = url;
  link.download = `${fileStub}.csv`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
