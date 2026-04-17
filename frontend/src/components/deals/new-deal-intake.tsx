"use client";

import { ChangeEvent, DragEvent, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { ArrowRight, FilePlus2, ScanSearch, UploadCloud } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { WorkflowBanner } from "@/components/deals/workflow-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useDealsStore } from "@/lib/deals-store";
import {
  IntakeScanSummary,
  IntakeUploadInput,
  isSupportedStructuredFile,
} from "@/lib/local-pipeline";
import { uploadTemplates } from "@/lib/mock-data";
import { Deal, FileCategory, FileStatus, SourceFile } from "@/lib/types";
import { formatDate } from "@/lib/utils";
import { getWorkflowSnapshot } from "@/lib/workflow";

const categories: FileCategory[] = [
  "Financials",
  "KPI Reports",
  "Customer Data",
  "Legal / Misc",
];

interface StagedUploadFile extends SourceFile {
  file?: File;
}

interface NewDealIntakeProps {
  deal?: Deal;
}

function getInitialScanSummary(deal?: Deal): IntakeScanSummary | null {
  if (!deal) {
    return null;
  }

  const issueCount =
    deal.qualityPanel.missingHeaders +
    deal.qualityPanel.duplicateFiles +
    deal.qualityPanel.unreadablePages +
    deal.qualityPanel.unitAmbiguity;

  return {
    fileCount: deal.sourceFiles.length,
    financialTables: deal.extractedItems.length,
    possibleIssues: issueCount,
    readinessScore: deal.readinessScore,
  };
}

function buildStagedUpload(
  file: {
    name: string;
    fileType: string;
    detectedCategory: FileCategory;
    status?: FileStatus;
    file?: File;
  },
  owner: string,
): StagedUploadFile {
  return {
    id: `staged-${file.name}-${Math.random().toString(36).slice(2, 8)}`,
    name: file.name,
    fileType: file.fileType,
    uploadDate: new Date().toISOString(),
    detectedCategory: file.detectedCategory,
    status: file.status ?? "Connected",
    pages: file.fileType === "PDF" ? 24 : 1,
    owner,
    supportedForParsing: isSupportedStructuredFile(file.name),
    file: file.file,
  };
}

export function NewDealIntake({ deal }: NewDealIntakeProps = {}) {
  const isWorkspaceMode = Boolean(deal);
  const workflow = deal ? getWorkflowSnapshot(deal) : null;
  const intakeStage = workflow?.stages.find((stage) => stage.key === "intake");
  const { createDealFromUploads, scanIntoDeal } = useDealsStore();

  const [dealName, setDealName] = useState(deal?.targetCompanyName ?? "Project Cedar");
  const [sector, setSector] = useState(deal?.sector ?? "Business Services");
  const [selectedCategory, setSelectedCategory] = useState<FileCategory>("Financials");
  const [stagedUploads, setStagedUploads] = useState<StagedUploadFile[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [scanSummary, setScanSummary] = useState<IntakeScanSummary | null>(
    getInitialScanSummary(deal),
  );
  const [activityNote, setActivityNote] = useState(
    "CSV and XLSX files are now parsed locally in the browser. PDF and other unsupported types still stay as placeholders in this sprint.",
  );
  const [createdDealId, setCreatedDealId] = useState<string | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const manifest = useMemo(
    () => (deal ? [...deal.sourceFiles, ...stagedUploads] : stagedUploads),
    [deal, stagedUploads],
  );
  const pendingUploads = stagedUploads.map(
    (file): IntakeUploadInput => ({
      file: file.file,
      name: file.name,
      fileType: file.fileType,
      detectedCategory: file.detectedCategory,
      uploadDate: file.uploadDate,
      status: file.status,
    }),
  );

  const addFiles = (
    files: {
      name: string;
      fileType: string;
      detectedCategory?: FileCategory;
      status?: FileStatus;
      file?: File;
    }[],
  ) => {
    setStagedUploads((current) => [
      ...current,
      ...files.map((file) =>
        buildStagedUpload(
          {
            ...file,
            detectedCategory: file.detectedCategory ?? selectedCategory,
          },
          isWorkspaceMode ? "Deal team" : "You",
        ),
      ),
    ]);
  };

  const handleFileSelection = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    addFiles(
      files.map((file) => ({
        name: file.name,
        fileType: file.name.split(".").pop()?.toUpperCase() ?? "FILE",
        file,
      })),
    );
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const files = Array.from(event.dataTransfer.files ?? []);
    addFiles(
      files.map((file) => ({
        name: file.name,
        fileType: file.name.split(".").pop()?.toUpperCase() ?? "FILE",
        file,
      })),
    );
  };

  const addTemplate = () => {
    addFiles(
      uploadTemplates.map((template) => ({
        name: template.name,
        fileType: template.fileType,
        detectedCategory: template.detectedCategory,
        status: template.status,
      })),
    );
  };

  const runInitialScan = async () => {
    if (pendingUploads.length === 0) {
      return;
    }

    setIsScanning(true);
    setCreatedDealId(null);

    try {
      if (deal) {
        const summary = await scanIntoDeal(deal.id, pendingUploads);
        setScanSummary(summary);
        setActivityNote(
          `${pendingUploads.length} new files were scanned locally and merged into this deal workspace.`,
        );
      } else {
        const result = await createDealFromUploads({
          dealName,
          sector,
          uploads: pendingUploads,
        });
        setCreatedDealId(result.dealId);
        setScanSummary(result.scanSummary);
        setActivityNote(
          `${pendingUploads.length} files were parsed locally and saved into a new deal workspace.`,
        );
      }

      setStagedUploads([]);
    } finally {
      setIsScanning(false);
    }
  };

  return (
    <div className="space-y-8">
      {isWorkspaceMode && intakeStage ? (
        <WorkflowBanner
          step={intakeStage.step}
          label={intakeStage.label}
          status={intakeStage.status}
          message="Intake stays accessible here whenever more source files arrive and need to be folded into the live workflow."
          helperText="CSV and XLSX files are parsed locally in the browser, then pushed directly into extraction, mapping, review, and output state."
          metrics={[
            { label: "Files in room", value: `${manifest.length} connected` },
            { label: "Pending scan", value: `${stagedUploads.length} new files` },
            { label: "Next step", value: "Extraction" },
          ]}
          actions={
            <Button asChild>
              <Link href={`/deals/${deal!.id}/extraction`}>
                Continue to Extraction
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          }
        />
      ) : null}

      <PageIntro
        eyebrow={isWorkspaceMode ? "Intake" : "New deal intake"}
        title={
          isWorkspaceMode
            ? "Add real local files into the existing deal workflow."
            : "Create a new deal from real local CSV and XLSX files."
        }
        description={
          isWorkspaceMode
            ? "New CSV and XLSX uploads are parsed locally and merged into this deal’s extraction, mapping, review, and output state. Unsupported file types remain visible as placeholders."
            : "This intake page now supports a semi-real local prototype path: upload CSV/XLSX files, parse them in the browser, and create a deal workspace from the resulting extraction and mapping state."
        }
        actions={
          createdDealId ? (
            <Button asChild>
              <Link href={`/deals/${createdDealId}`}>
                Open Created Workspace
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          ) : !isWorkspaceMode ? (
            <Button asChild variant="secondary">
              <Link href="/deals/northstar-software">
                Open Sample Workspace
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          ) : null
        }
      />

      <div className="rounded-2xl border border-border bg-surface-muted px-4 py-3 text-sm text-muted-foreground">
        {activityNote}
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.05fr_1.4fr]">
        <Card>
          <CardHeader>
            <CardTitle>Deal setup</CardTitle>
            <CardDescription>Simple metadata for the local prototype workspace.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="deal-name">Deal name</Label>
              <Input
                id="deal-name"
                value={dealName}
                onChange={(event) => setDealName(event.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="sector">Sector</Label>
              <Input
                id="sector"
                value={sector}
                onChange={(event) => setSector(event.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="category">Assign uploaded files to</Label>
              <Select
                id="category"
                value={selectedCategory}
                onChange={(event) => setSelectedCategory(event.target.value as FileCategory)}
              >
                {categories.map((category) => (
                  <option key={category} value={category}>
                    {category}
                  </option>
                ))}
              </Select>
            </div>
            <div className="rounded-2xl border border-dashed border-border-strong bg-surface-muted p-5">
              <div className="flex items-start gap-3">
                <FilePlus2 className="mt-0.5 h-5 w-5 text-accent" />
                <div className="space-y-1">
                  <p className="font-semibold">Sprint behavior</p>
                  <p className="text-sm leading-6 text-muted-foreground">
                    `.csv` and `.xlsx` are parsed locally. PDFs and other unsupported files still
                    show up in the manifest, but they remain placeholders for now.
                  </p>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card
            className={`border-dashed transition ${dragActive ? "border-accent bg-white" : "border-border-strong"}`}
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
            <CardContent className="mt-0 flex flex-col items-center justify-center gap-4 py-12 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-border bg-white/90">
                <UploadCloud className="h-7 w-7 text-accent" />
              </div>
              <div className="space-y-2">
                <h2 className="text-xl font-semibold">
                  {isWorkspaceMode
                    ? "Add more local files into this intake manifest"
                    : "Upload local CSV or XLSX files"}
                </h2>
                <p className="max-w-xl text-sm leading-6 text-muted-foreground">
                  Supported files are parsed locally in the browser during the intake scan. Sample
                  templates and unsupported files still remain demo placeholders.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-3">
                <Button onClick={() => fileInputRef.current?.click()} variant="secondary">
                  Select files
                </Button>
                <Button onClick={addTemplate} variant="outline">
                  Load sample file set
                </Button>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={handleFileSelection}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>File manifest</CardTitle>
              <CardDescription>
                Files remain visible before scanning, and structured uploads can now feed a real
                local parse path.
              </CardDescription>
            </CardHeader>
            <CardContent className="mt-0 space-y-4">
              {manifest.length === 0 ? (
                <div className="rounded-2xl border border-border bg-surface-muted p-6 text-sm text-muted-foreground">
                  No files staged yet. Add a sample file set or select local files to populate the
                  manifest.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>File name</TableHead>
                        <TableHead>Type</TableHead>
                        <TableHead>Upload date</TableHead>
                        <TableHead>Detected category</TableHead>
                        <TableHead>Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {manifest.map((file) => (
                        <TableRow key={file.id}>
                          <TableCell className="font-medium">
                            <div className="space-y-1">
                              <div>{file.name}</div>
                              <div className="text-xs text-muted-foreground">
                                {file.supportedForParsing ? "Real local parse supported" : "Mock placeholder for now"}
                              </div>
                            </div>
                          </TableCell>
                          <TableCell>{file.fileType}</TableCell>
                          <TableCell>{formatDate(file.uploadDate)}</TableCell>
                          <TableCell>{file.detectedCategory}</TableCell>
                          <TableCell>
                            <StatusBadge value={file.status} />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              <Button
                disabled={pendingUploads.length === 0 || isScanning}
                onClick={runInitialScan}
              >
                <ScanSearch className="h-4 w-4" />
                {isScanning
                  ? "Running local scan..."
                  : isWorkspaceMode
                    ? "Re-run Intake Scan"
                    : "Run Initial Scan"}
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>

      {scanSummary ? (
        <Card>
          <CardHeader>
            <CardTitle>Initial scan summary</CardTitle>
            <CardDescription>
              Local scan output before extraction and mapping continue downstream.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <Card className="border-border bg-white/80">
              <CardHeader>
                <CardDescription>Files detected</CardDescription>
                <CardTitle className="text-3xl">{scanSummary.fileCount}</CardTitle>
              </CardHeader>
            </Card>
            <Card className="border-border bg-white/80">
              <CardHeader>
                <CardDescription>Financial tables found</CardDescription>
                <CardTitle className="text-3xl">{scanSummary.financialTables}</CardTitle>
              </CardHeader>
            </Card>
            <Card className="border-border bg-white/80">
              <CardHeader>
                <CardDescription>Possible issues</CardDescription>
                <CardTitle className="text-3xl">{scanSummary.possibleIssues}</CardTitle>
              </CardHeader>
            </Card>
            <Card className="border-border bg-white/80">
              <CardHeader>
                <CardDescription>Readiness score</CardDescription>
                <CardTitle className="text-3xl">{scanSummary.readinessScore}</CardTitle>
              </CardHeader>
            </Card>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
