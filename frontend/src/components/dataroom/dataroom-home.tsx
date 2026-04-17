"use client";

import { ChangeEvent, DragEvent, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { ArrowRight, FilePlus2, UploadCloud } from "lucide-react";

import { DataroomStepStrip } from "@/components/dataroom/dataroom-step-strip";
import { StatusBadge } from "@/components/deals/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDealsStore } from "@/lib/deals-store";
import { IntakeUploadInput } from "@/lib/local-pipeline";
import { FileCategory } from "@/lib/types";
import { formatDate } from "@/lib/utils";

interface ImportFile {
  id: string;
  file?: File;
  name: string;
  fileType: string;
  category: FileCategory;
  uploadedAt: string;
}

const backendSupportedExtensions = new Set(["csv", "xlsx", "xls", "xlsm", "pdf", "docx", "txt"]);

function isBackendSupportedFile(fileName: string) {
  const extension = fileName.toLowerCase().split(".").pop() ?? "";
  return backendSupportedExtensions.has(extension);
}

function inferCategory(fileName: string): FileCategory {
  const normalized = fileName.toLowerCase();

  if (normalized.includes("customer") || normalized.includes("arr") || normalized.includes("churn")) {
    return "Customer Data";
  }

  if (normalized.includes("kpi") || normalized.includes("board") || normalized.includes("operating")) {
    return "KPI Reports";
  }

  if (normalized.includes("legal") || normalized.includes("doc")) {
    return "Legal / Misc";
  }

  return "Financials";
}

function buildImportFile(file: File): ImportFile {
  return {
    id: `import-${file.name}-${Math.random().toString(36).slice(2, 8)}`,
    file,
    name: file.name,
    fileType: file.name.split(".").pop()?.toUpperCase() ?? "FILE",
    category: inferCategory(file.name),
    uploadedAt: new Date().toISOString(),
  };
}

export function DataroomHome() {
  const router = useRouter();
  const { createDealFromUploads } = useDealsStore();
  const [importLabel, setImportLabel] = useState("New Dataroom Batch");
  const [files, setFiles] = useState<ImportFile[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const supportedCount = files.filter((file) => isBackendSupportedFile(file.name)).length;
  const unsupportedCount = files.length - supportedCount;
  const pendingUploads = files.map(
    (file): IntakeUploadInput => ({
      file: file.file,
      name: file.name,
      fileType: file.fileType,
      detectedCategory: file.category,
      uploadDate: file.uploadedAt,
      status: "Connected",
    }),
  );
  const detectedTypes = useMemo(
    () => Array.from(new Set(files.map((file) => file.fileType))),
    [files],
  );

  const addFiles = (selectedFiles: File[]) => {
    setErrorMessage(null);
    setFiles((current) => [...current, ...selectedFiles.map(buildImportFile)]);
  };

  const handleSelection = (event: ChangeEvent<HTMLInputElement>) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    addFiles(Array.from(event.dataTransfer.files ?? []));
  };

  const startProcessing = async () => {
    if (pendingUploads.length === 0) {
      return;
    }

    setErrorMessage(null);
    setIsProcessing(true);

    try {
      const result = await createDealFromUploads({
        dealName: importLabel,
        sector: "Dataroom Import",
        uploads: pendingUploads,
      });

      router.push(`/process/${result.dealId}`);
    } catch (error) {
      setErrorMessage(
        error instanceof Error
          ? error.message
          : "The Python processing pipeline could not start. Make sure `angelic-api` is running and try again.",
      );
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <div className="page-shell space-y-8">
      <section className="hero-panel animate-fade-up px-6 py-7 sm:px-8 sm:py-8">
        <div className="grid gap-8 xl:grid-cols-[1.35fr_0.8fr] xl:items-end">
          <div className="space-y-4">
            <div className="text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
              Narrow v1
            </div>
            <div className="space-y-3">
              <h1 className="max-w-4xl text-4xl font-semibold tracking-tight text-foreground sm:text-5xl">
                Angelic Dataroom
              </h1>
              <p className="max-w-2xl text-base leading-7 text-muted-foreground">
                Import source files, extract key financial data, and export a clean databook workbook.
              </p>
              <p className="max-w-3xl text-sm leading-7 text-muted-foreground">
                Drop files in, let the system process them, define what each item means, apply deterministic mapping and formulas,
                and get a source-aware output that is ready to reuse on the next deal.
              </p>
            </div>
            <div className="flex flex-wrap gap-2 pt-2 text-xs uppercase tracking-[0.16em] text-muted-foreground">
              <span className="rounded-full border border-border bg-white/75 px-3 py-1 shadow-[0_10px_20px_rgba(19,32,45,0.05)]">
                Multi-file import
              </span>
              <span className="rounded-full border border-border bg-white/75 px-3 py-1 shadow-[0_10px_20px_rgba(19,32,45,0.05)]">
                Deterministic formulas
              </span>
              <span className="rounded-full border border-border bg-white/75 px-3 py-1 shadow-[0_10px_20px_rgba(19,32,45,0.05)]">
                Traceable export
              </span>
            </div>
          </div>

          <div className="surface-panel glow-accent grid gap-3 p-5 animate-fade-up animate-delay-2 sm:grid-cols-3 xl:grid-cols-1">
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Supported now
              </div>
              <div className="mt-2 text-xl font-semibold">CSV, XLSX, PDF, DOCX, TXT</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                The Python pipeline handles spreadsheets and readable documents. Spreadsheets are still the strongest starting point.
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Processing shape
              </div>
              <div className="mt-2 text-xl font-semibold">Import to workbook</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                Extraction, definition, formulas, and export stay in one clear path.
              </div>
            </div>
            <div className="metric-panel px-4 py-4">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                Review posture
              </div>
              <div className="mt-2 text-xl font-semibold">Secondary only</div>
              <div className="mt-1 text-sm leading-6 text-muted-foreground">
                Exceptions stay available without taking over the main product story.
              </div>
            </div>
          </div>
        </div>
      </section>

      <div className="animate-fade-up animate-delay-2">
        <DataroomStepStrip currentStep="import" />
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.4fr_0.9fr]">
        <Card
          className={`lift-card data-grid animate-fade-up animate-delay-3 border-dashed ${dragActive ? "border-accent bg-white shadow-[0_30px_70px_rgba(31,57,80,0.12)]" : "border-border-strong bg-white/[0.84]"}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            event.preventDefault();
            setDragActive(false);
          }}
          onDrop={handleDrop}
        >
          <CardContent className="relative mt-0 flex flex-col items-center justify-center gap-5 py-14 text-center">
            <div className="absolute inset-x-8 top-0 h-px bg-[linear-gradient(90deg,transparent,rgba(31,57,80,0.2),transparent)]" />
            <div className="flex h-[4.5rem] w-[4.5rem] items-center justify-center rounded-[1.4rem] border border-border bg-white/[0.92] shadow-[0_20px_36px_rgba(19,32,45,0.08)]">
              <UploadCloud className="h-7 w-7 text-accent" />
            </div>
            <div className="space-y-2">
              <h2 className="text-2xl font-semibold">Import files</h2>
              <p className="max-w-2xl text-sm leading-7 text-muted-foreground">
                Upload a messy dataroom folder subset or drag multiple files in now. The browser
                sends them into the Python pipeline, which parses supported files, uses Gemini for
                extraction where needed, and writes the workbook deterministically.
              </p>
            </div>
            <div className="flex flex-wrap justify-center gap-3">
              <Button onClick={() => fileInputRef.current?.click()} variant="secondary">
                Select files
              </Button>
              <Button onClick={startProcessing} disabled={files.length === 0 || isProcessing} className="shadow-[0_18px_38px_rgba(31,57,80,0.18)]">
                {isProcessing ? "Processing files..." : "Process Files"}
                <ArrowRight className="h-4 w-4" />
              </Button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={handleSelection}
            />
            <div className="flex flex-wrap justify-center gap-2 text-xs uppercase tracking-[0.16em] text-muted-foreground">
              <span className="rounded-full border border-border bg-white/80 px-3 py-1">Supported: CSV</span>
              <span className="rounded-full border border-border bg-white/80 px-3 py-1">Supported: XLSX</span>
              <span className="rounded-full border border-border bg-white/80 px-3 py-1">Supported: PDF / DOCX / TXT</span>
              <span className="rounded-full border border-border bg-white/80 px-3 py-1">Secondary review available if needed</span>
            </div>
            {errorMessage ? (
              <div className="surface-panel w-full max-w-2xl border border-[rgba(163,94,76,0.24)] bg-[rgba(170,112,93,0.08)] px-4 py-3 text-sm leading-6 text-foreground">
                {errorMessage}
              </div>
            ) : null}
            <div className="grid w-full max-w-2xl gap-3 pt-2 sm:grid-cols-3">
              <div className="metric-panel px-4 py-3">
                <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Files staged</div>
                <div className="mt-2 text-2xl font-semibold">{files.length}</div>
              </div>
              <div className="metric-panel px-4 py-3">
                <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Ready for backend</div>
                <div className="mt-2 text-2xl font-semibold">{supportedCount}</div>
              </div>
              <div className="metric-panel px-4 py-3">
                <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Unsupported</div>
                <div className="mt-2 text-2xl font-semibold">{unsupportedCount}</div>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
          <CardHeader>
            <CardTitle>Import setup</CardTitle>
            <CardDescription>Keep the first interaction simple and close to the work product.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <label className="text-sm font-medium text-foreground" htmlFor="import-label">
                Import label
              </label>
              <Input
                id="import-label"
                value={importLabel}
                onChange={(event) => setImportLabel(event.target.value)}
              />
            </div>
            <div className="rounded-2xl border border-border bg-surface-muted p-4">
              <div className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                3-step flow
              </div>
              <div className="mt-3 space-y-3 text-sm text-muted-foreground">
                <div className="surface-panel flex items-center gap-3 px-3 py-3">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full border border-border-strong bg-white text-xs font-semibold text-foreground">1</span>
                  <span>Import source files</span>
                </div>
                <div className="surface-panel flex items-center gap-3 px-3 py-3">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full border border-border-strong bg-white text-xs font-semibold text-foreground">2</span>
                  <span>Run Gemini extraction, deterministic formulas, and workbook writing</span>
                </div>
                <div className="surface-panel flex items-center gap-3 px-3 py-3">
                  <span className="flex h-8 w-8 items-center justify-center rounded-full border border-border-strong bg-white text-xs font-semibold text-foreground">3</span>
                  <span>Export a clean databook</span>
                </div>
              </div>
            </div>
            <div className="surface-panel border-dashed border-border-strong bg-white/80 p-4">
              <div className="flex items-start gap-3">
                <FilePlus2 className="mt-0.5 h-5 w-5 text-accent" />
                <div className="space-y-1">
                  <p className="font-semibold">Advanced review stays secondary</p>
                  <p className="text-sm leading-6 text-muted-foreground">
                    If the system flags unmapped rows or calculation issues, the user can open
                    review details later. It is not the main product story.
                  </p>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {files.length > 0 ? (
        <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
          <CardHeader>
            <CardTitle>Imported files</CardTitle>
            <CardDescription>
              {supportedCount} of {files.length} files are supported by the Python processing
              pipeline. Detected types: {detectedTypes.join(", ")}.
            </CardDescription>
          </CardHeader>
          <CardContent className="table-scroll mt-0 overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>File name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Detected category</TableHead>
                  <TableHead>Uploaded</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((file) => (
                  <TableRow key={file.id}>
                    <TableCell className="font-medium">{file.name}</TableCell>
                    <TableCell>{file.fileType}</TableCell>
                    <TableCell>{file.category}</TableCell>
                    <TableCell>{formatDate(file.uploadedAt)}</TableCell>
                    <TableCell>
                      <StatusBadge
                        value={
                          isBackendSupportedFile(file.name) ? "Ready to process" : "Unsupported"
                        }
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
