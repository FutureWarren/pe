import Link from "next/link";

import { ArrowLeft } from "lucide-react";

import { PageIntro } from "@/components/deals/page-intro";
import { StatusBadge } from "@/components/deals/status-badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Deal, OutputAsset } from "@/lib/types";
import { cn } from "@/lib/utils";

interface OutputPreviewViewProps {
  deal: Deal;
  output: OutputAsset;
}

export function OutputPreviewView({ deal, output }: OutputPreviewViewProps) {
  return (
    <div className="space-y-6">
      <PageIntro
        eyebrow="Output preview"
        title={output.name}
        description="Inspect the current local deliverable before exporting the databook-style CSV."
        actions={
          <Link
            className={cn(buttonVariants({ variant: "secondary" }))}
            href={`/deals/${deal.id}/outputs`}
          >
            <ArrowLeft className="h-4 w-4" />
            Back to Output Center
          </Link>
        }
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Status</CardDescription>
            <CardTitle className="text-xl">
              <StatusBadge value={output.status} />
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Completeness</CardDescription>
            <CardTitle className="text-3xl">{output.completeness}%</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Source linked</CardDescription>
            <CardTitle className="text-3xl">{output.sourceLinked ? "Yes" : "No"}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Review status</CardDescription>
            <CardTitle className="text-base leading-7">{output.reviewStatus}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {output.previewType === "table" && output.previewRows ? (
        <Card>
          <CardHeader>
            <CardTitle>Preview table</CardTitle>
            <CardDescription>
              Representative output rows with source trace references preserved.
            </CardDescription>
          </CardHeader>
          <CardContent className="table-scroll mt-0 overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Line item</TableHead>
                  <TableHead>Primary value</TableHead>
                  <TableHead>Secondary value</TableHead>
                  <TableHead>Trace</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {output.previewRows.map((row) => (
                  <TableRow key={`${row.item}-${row.valueA}`}>
                    <TableCell className="font-medium">{row.item}</TableCell>
                    <TableCell>{row.valueA}</TableCell>
                    <TableCell>{row.valueB ?? "—"}</TableCell>
                    <TableCell>{row.trace}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}

      {output.previewType === "sections" && output.previewSections ? (
        <div className="grid gap-4 lg:grid-cols-2">
          {output.previewSections.map((section) => (
            <Card key={section.heading}>
              <CardHeader>
                <CardTitle>{section.heading}</CardTitle>
                <CardDescription>Sample document-style content for review.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {section.bullets.map((bullet) => (
                  <div key={bullet} className="rounded-2xl border border-border bg-white/80 p-4 text-sm leading-7 text-muted-foreground">
                    {bullet}
                  </div>
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : null}
    </div>
  );
}
