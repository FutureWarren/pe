"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { LoaderCircle, Sparkles } from "lucide-react";

import { StatusBadge } from "@/components/deals/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { requestBackendMetricExplanation } from "@/lib/backend-pipeline";
import { AnalystExplainQuestion, Deal, FinalMetricRecord } from "@/lib/types";
import { cn, formatCurrency, formatInteger } from "@/lib/utils";

const QUESTION_BUTTONS: Array<{ label: string; question: AnalystExplainQuestion }> = [
  { label: "Show source", question: "source" },
  { label: "Why this source", question: "why_this_source" },
  { label: "Was this recalculated", question: "direct_or_derived" },
  { label: "Why confidence", question: "confidence" },
  { label: "Did this match other files", question: "compare_files" },
  { label: "Where do I verify", question: "where_to_verify" },
];

interface ModelInputWorkbenchProps {
  deal: Deal;
}

function formatMetricValue(record: FinalMetricRecord | undefined) {
  if (!record || record.finalValue === null) {
    return "";
  }

  if (record.unit === "%") {
    // Fractions (0.45) and already-in-points values (45) both mean 45%.
    const percent = Math.abs(record.finalValue) > 1.5 ? record.finalValue : record.finalValue * 100;
    return `${percent.toFixed(1)}%`;
  }
  if (record.unit === "count") {
    return formatInteger(record.finalValue);
  }
  return formatCurrency(record.finalValue);
}

function summarizeMetricRow(records: FinalMetricRecord[]) {
  const confidenceRank = { High: 0, Medium: 1, Low: 2 };
  const worstConfidence =
    records.length > 0
      ? [...records].sort((a, b) => confidenceRank[b.confidenceLevel] - confidenceRank[a.confidenceLevel])[0]
          .confidenceLevel
      : "";

  const status = records.some((record) => record.status === "Review") ? "Review" : "Ready";
  const validation =
    records.some((record) => record.validationResult === "Mismatch")
      ? "Mismatch"
      : records.some((record) => record.validationResult === "Formula")
        ? "Formula"
        : records.some((record) => record.validationResult === "Matched")
          ? "Matched"
          : records.some((record) => record.validationResult === "Single-source")
            ? "Single-source"
            : "";
  const note = records.find((record) => record.note)?.note ?? "";
  const unit = records.find((record) => record.unit)?.unit ?? null;

  return {
    confidence: worstConfidence,
    status,
    validation,
    note,
    unit,
  };
}

function unitDisplay(unit: FinalMetricRecord["unit"]) {
  if (unit === "%") return "%";
  if (unit === "count") return "count";
  if (unit === "USD_thousands") return "USD '000";
  if (unit === "ratio") return "ratio";
  return "USD";
}

export function ModelInputWorkbench({ deal }: ModelInputWorkbenchProps) {
  const analystBundle = deal.analystBundle;
  const runId = deal.backendRun?.runId;
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [activeQuestion, setActiveQuestion] = useState<AnalystExplainQuestion>("summary");
  const [answer, setAnswer] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const askSeqRef = useRef(0);

  const metricsByMetric = useMemo(() => {
    const index = new Map<string, Map<string, FinalMetricRecord>>();
    for (const metric of analystBundle?.metrics ?? []) {
      if (!index.has(metric.metricKey)) {
        index.set(metric.metricKey, new Map());
      }
      index.get(metric.metricKey)!.set(metric.periodKey, metric);
    }
    return index;
  }, [analystBundle]);

  const selectableRecords = useMemo(
    () =>
      (analystBundle?.metrics ?? []).filter(
        (metric) => metric.finalValue !== null || metric.directOrDerived === "derived",
      ),
    [analystBundle],
  );

  useEffect(() => {
    if (!selectedKey && selectableRecords.length > 0) {
      const first = selectableRecords[0];
      setSelectedKey(`${first.metricKey}:${first.periodKey}`);
    }
  }, [selectableRecords, selectedKey]);

  const selectedRecord =
    selectableRecords.find((metric) => `${metric.metricKey}:${metric.periodKey}` === selectedKey) ?? null;

  async function ask(question: AnalystExplainQuestion) {
    if (!runId || !selectedRecord) {
      return;
    }

    // Sequence guard: switching metrics fires a new request while older ones
    // may still be in flight — a slow older response must not overwrite the
    // answer for the newly selected metric/question.
    const seq = ++askSeqRef.current;
    setActiveQuestion(question);
    setLoading(true);
    setError(null);

    try {
      const response = await requestBackendMetricExplanation({
        runId,
        metricKey: selectedRecord.metricKey,
        periodKey: selectedRecord.periodKey,
        question,
      });
      if (askSeqRef.current === seq) {
        setAnswer(response.answer);
      }
    } catch (nextError) {
      if (askSeqRef.current === seq) {
        setError(nextError instanceof Error ? nextError.message : "Unable to load the explanation.");
      }
    } finally {
      if (askSeqRef.current === seq) {
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    if (!selectedRecord || !runId) {
      setAnswer("");
      return;
    }
    void ask("summary");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRecord?.metricKey, selectedRecord?.periodKey, runId]);

  if (!analystBundle) {
    return (
      <Card className="lift-card bg-white/[0.88]">
        <CardHeader>
          <CardTitle>Model input preview</CardTitle>
          <CardDescription>
            The backend has not attached a canonical analyst bundle to this run yet.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    // The metrics table needs ~950px with a few periods; splitting at xl left it
    // permanently horizontal-scrolling on 1280-1440px laptops. Stack until 2xl.
    <div className="grid gap-6 2xl:grid-cols-[1.35fr_0.95fr]">
      <Card className="lift-card animate-fade-up animate-delay-4 bg-white/[0.88]">
        <CardHeader>
          <CardTitle>Model input preview</CardTitle>
          <CardDescription>
            This is the analyst-facing output. Click any number to ask where it came from and why it can be trusted.
          </CardDescription>
        </CardHeader>
        <CardContent className="table-scroll mt-0 overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Metric</TableHead>
                {analystBundle.periodOrder.map((period) => (
                  <TableHead key={period}>{period}</TableHead>
                ))}
                <TableHead>Unit</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Validation</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Notes</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {analystBundle.metricOrder.map((metricKey) => {
                const periodRecords = metricsByMetric.get(metricKey) ?? new Map();
                const rowRecords = analystBundle.periodKeys
                  .map((periodKey) => periodRecords.get(periodKey))
                  .filter((record): record is FinalMetricRecord => Boolean(record));
                const summary = summarizeMetricRow(rowRecords);
                const label = rowRecords[0]?.metricName ?? metricKey;

                return (
                  <TableRow key={metricKey}>
                    <TableCell className="font-medium">{label}</TableCell>
                    {analystBundle.periodKeys.map((periodKey) => {
                      const record = periodRecords.get(periodKey);
                      const cellKey = record ? `${record.metricKey}:${record.periodKey}` : `${metricKey}:${periodKey}`;
                      const selected = selectedKey === cellKey;

                      return (
                        <TableCell key={periodKey}>
                          {record && record.finalValue !== null ? (
                            <button
                              type="button"
                              onClick={() => setSelectedKey(cellKey)}
                              aria-pressed={selected}
                              className={cn(
                                "min-w-[92px] rounded-lg border px-2 py-1 text-right font-mono text-sm tabular-nums transition-[border-color,background-color,box-shadow] duration-200",
                                selected
                                  ? "border-accent bg-accent/10 text-foreground shadow-sm"
                                  : "border-border bg-background/80 text-foreground hover:border-accent/40 hover:bg-accent/5",
                              )}
                            >
                              {formatMetricValue(record)}
                            </button>
                          ) : (
                            /* Same box metrics as a valued cell so sparse rows don't jitter. */
                            <span className="inline-block min-w-[92px] px-2 py-1 text-right font-mono text-sm text-muted-foreground">
                              —
                            </span>
                          )}
                        </TableCell>
                      );
                    })}
                    <TableCell>{unitDisplay(summary.unit)}</TableCell>
                    <TableCell>{summary.confidence}</TableCell>
                    <TableCell>{summary.validation}</TableCell>
                    <TableCell>
                      <StatusBadge value={summary.status || "Ready"} />
                    </TableCell>
                    <TableCell className="max-w-[220px] text-sm text-muted-foreground">
                      {summary.note || ""}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card className="lift-card animate-fade-up animate-delay-5 bg-white/[0.88]">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent" />
            Ask about this number
          </CardTitle>
          <CardDescription>
            Grounded explanations only. Every answer comes from the canonical source record, not from free-form guessing.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {selectedRecord ? (
            <>
              <div className="surface-panel space-y-3 p-4">
                <div className="space-y-1">
                  <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Selected value</div>
                  <div className="text-xl font-semibold">
                    {selectedRecord.metricName} · {selectedRecord.period}
                  </div>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Value</div>
                    <div className="mt-1 font-semibold">{formatMetricValue(selectedRecord)}</div>
                  </div>
                  <div>
                    <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Status</div>
                    <div className="mt-1">
                      <StatusBadge value={selectedRecord.status} />
                    </div>
                  </div>
                  <div>
                    <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Confidence</div>
                    <div className="mt-1 font-semibold">{selectedRecord.confidenceLevel}</div>
                  </div>
                  <div>
                    <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Validation</div>
                    <div className="mt-1 font-semibold">{selectedRecord.validationResult}</div>
                  </div>
                </div>
                <div className="text-sm text-muted-foreground">
                  {selectedRecord.selectedSource
                    ? `Primary source: ${[
                        selectedRecord.selectedSource.file,
                        selectedRecord.selectedSource.tab,
                        selectedRecord.selectedSource.range,
                      ]
                        .filter(Boolean)
                        .join(" · ")}`
                    : selectedRecord.derivationFormula
                      ? `Derived as ${selectedRecord.derivationFormula}`
                      : "No primary source is attached to this value."}
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={activeQuestion === "summary" ? "default" : "secondary"}
                  aria-pressed={activeQuestion === "summary"}
                  disabled={loading}
                  onClick={() => void ask("summary")}
                >
                  Summary
                </Button>
                {QUESTION_BUTTONS.map((item) => (
                  <Button
                    key={item.question}
                    type="button"
                    size="sm"
                    variant={activeQuestion === item.question ? "default" : "secondary"}
                    aria-pressed={activeQuestion === item.question}
                    disabled={loading}
                    onClick={() => void ask(item.question)}
                  >
                    {item.label}
                  </Button>
                ))}
              </div>

              <div className="surface-panel min-h-[220px] space-y-3 p-4">
                <div className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                  Analyst explanation
                </div>
                {loading ? (
                  <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
                    <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" />
                    Loading grounded explanation...
                  </div>
                ) : error ? (
                  <div role="alert" className="text-sm text-danger">{error}</div>
                ) : (
                  <div className="space-y-3 text-sm leading-7 text-foreground">
                    <p>{answer || "Select a number to load an explanation."}</p>
                    {selectedRecord.crossCheckLog.length > 0 ? (
                      <div className="space-y-1 text-muted-foreground">
                        <div className="text-xs uppercase tracking-[0.16em]">Cross-checks logged</div>
                        {selectedRecord.crossCheckLog.slice(0, 3).map((line) => (
                          <p key={line}>{line}</p>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="surface-panel p-4 text-sm text-muted-foreground">
              No canonical metric is available to explain yet.
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
