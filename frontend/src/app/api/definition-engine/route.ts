import { NextRequest, NextResponse } from "next/server";

import { standardTags } from "@/lib/mock-data";
import type { DefinitionRequestRow, GeminiDefinitionResult } from "@/lib/llm-definition";

export const runtime = "nodejs";

const unmappedCategory = "Unmapped";
const allowedCategories = [unmappedCategory, ...standardTags];
const allowedStatuses = ["Approved", "Pending", "Needs Review"] as const;

function chunkArray<T>(values: T[], size: number) {
  const chunks: T[][] = [];

  for (let index = 0; index < values.length; index += size) {
    chunks.push(values.slice(index, index + size));
  }

  return chunks;
}

function getGeminiApiKey() {
  return process.env.ANGELIC_GEMINI_API_KEY ?? process.env.GEMINI_API_KEY ?? "";
}

function getGeminiModel() {
  return process.env.ANGELIC_GEMINI_MODEL ?? process.env.GEMINI_MODEL ?? "gemini-2.5-pro";
}

function sanitizeCategory(value: string) {
  return allowedCategories.includes(value) ? value : unmappedCategory;
}

function sanitizeStatus(value: string, mappedCategory: string) {
  if (mappedCategory === unmappedCategory) {
    return "Needs Review";
  }

  return allowedStatuses.includes(value as (typeof allowedStatuses)[number])
    ? (value as (typeof allowedStatuses)[number])
    : "Pending";
}

function sanitizeInterpretation(
  row: DefinitionRequestRow,
  interpretation: Partial<GeminiDefinitionResult>,
): GeminiDefinitionResult {
  const mappedCategory = sanitizeCategory(interpretation.mappedCategory ?? unmappedCategory);

  return {
    mappingRowId: row.mappingRowId,
    mappedCategory,
    reviewStatus: sanitizeStatus(interpretation.reviewStatus ?? "Pending", mappedCategory),
    confidence: Math.max(0, Math.min(100, Math.round(interpretation.confidence ?? 60))),
    definition:
      interpretation.definition?.trim() ||
      `Interpreted ${row.rawLabel} into ${mappedCategory}.`,
    rationale:
      interpretation.rationale?.trim() ||
      `Gemini interpreted "${row.rawLabel}" from ${row.sourceFileName} as ${mappedCategory}.`,
    directOrDerivedHint:
      interpretation.directOrDerivedHint === "Derived" ? "Derived" : "Direct",
    dependencyCandidates:
      (interpretation.dependencyCandidates ?? [])
        .filter(Boolean)
        .map((value) => value.trim())
        .slice(0, 6),
  };
}

function buildPrompt(rows: DefinitionRequestRow[]) {
  return `
You are classifying extracted financial rows for a private equity databook workflow.

Your job is to interpret what each row means so deterministic code can map inputs, calculate formulas, and write a workbook.

These rows already passed a deterministic scope gate as likely core financial rows or KPI inputs.
Do not try to rescue supporting detail, duplicate-supporting rows, or table warnings into the core databook taxonomy.

Allowed mapped categories:
${allowedCategories.join(", ")}

Allowed review statuses:
Approved, Pending, Needs Review

Rules:
- Be conservative. If the row meaning is ambiguous, return "Needs Review" or "Unmapped".
- Prioritize correct P&L classification over broad matching.
- Prefer deferring upstream rather than forcing a false core mapping.
- For clean P&L rows, correctly distinguish Revenue, COGS, Operating Expenses, Gross Profit, and EBITDA.
- KPI ratios and operating KPIs must stay in KPI-safe categories. Do not force Net Revenue Retention, churn, retention, NRR, GRR, or similar ratio rows into Revenue, COGS, Operating Expenses, Gross Profit, or EBITDA.
- ARR is a KPI metric, not the core Revenue line.
- Do not confuse workforce counts with currency metrics.
- Headcount must only be used for actual employee/FTE counts, never revenue or expense amounts.
- Capital expenditures, capital investment, and CapEx rows should favor CapEx and must not be mapped to Headcount.
- "Gross Profit" and "EBITDA" are formula-sensitive metrics. If a source row explicitly reports them, you may map them there, but do not misclassify component rows as those metrics.
- If a row still looks like supporting detail or a duplicate-supporting row even after the scope gate, return "Unmapped" and "Needs Review" instead of stretching it into a core category.
- Use source file name, sheet name, raw label, raw value, and period together.
- Keep rationales short and operational.
- Return exactly one interpretation per mappingRowId.

Rows:
${JSON.stringify(rows, null, 2)}
  `.trim();
}

function buildResponseSchema() {
  return {
    type: "object",
    properties: {
      interpretations: {
        type: "array",
        items: {
          type: "object",
          properties: {
            mappingRowId: { type: "string" },
            mappedCategory: {
              type: "string",
              enum: allowedCategories,
            },
            reviewStatus: {
              type: "string",
              enum: [...allowedStatuses],
            },
            confidence: { type: "number" },
            definition: { type: "string" },
            rationale: { type: "string" },
            directOrDerivedHint: {
              type: "string",
              enum: ["Direct", "Derived"],
            },
            dependencyCandidates: {
              type: "array",
              items: { type: "string" },
            },
          },
          required: [
            "mappingRowId",
            "mappedCategory",
            "reviewStatus",
            "confidence",
            "definition",
            "rationale",
            "directOrDerivedHint",
            "dependencyCandidates",
          ],
        },
      },
    },
    required: ["interpretations"],
  };
}

async function callGeminiForChunk(
  apiKey: string,
  model: string,
  rows: DefinitionRequestRow[],
) {
  const response = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": apiKey,
      },
      body: JSON.stringify({
        contents: [
          {
            parts: [{ text: buildPrompt(rows) }],
          },
        ],
        generationConfig: {
          temperature: 0.1,
          responseMimeType: "application/json",
          responseJsonSchema: buildResponseSchema(),
        },
      }),
    },
  );

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Gemini definition call failed: ${response.status} ${errorText}`);
  }

  const payload = (await response.json()) as {
    candidates?: Array<{
      content?: {
        parts?: Array<{ text?: string }>;
      };
    }>;
  };

  const text = payload.candidates?.[0]?.content?.parts
    ?.map((part) => part.text ?? "")
    .join("")
    .trim();

  if (!text) {
    throw new Error("Gemini definition call returned an empty response.");
  }

  const parsed = JSON.parse(text) as {
    interpretations?: Partial<GeminiDefinitionResult>[];
  };

  return (parsed.interpretations ?? []).map((interpretation) => {
    const row = rows.find((entry) => entry.mappingRowId === interpretation.mappingRowId);

    if (!row) {
      return null;
    }

    return sanitizeInterpretation(row, interpretation);
  }).filter((item): item is GeminiDefinitionResult => Boolean(item));
}

export async function POST(request: NextRequest) {
  const apiKey = getGeminiApiKey();
  const model = getGeminiModel();

  if (!apiKey) {
    return NextResponse.json({
      enabled: false,
      provider: "deterministic",
      fallbackReason: "missing_gemini_api_key",
      results: [],
    });
  }

  const payload = (await request.json()) as { rows?: DefinitionRequestRow[] };
  const rows = payload.rows ?? [];

  if (!Array.isArray(rows) || rows.length === 0) {
    return NextResponse.json(
      {
        enabled: false,
        provider: "deterministic",
        fallbackReason: "no_rows",
        results: [],
      },
      { status: 400 },
    );
  }

  try {
    const chunks = chunkArray(rows, 24);
    const results: GeminiDefinitionResult[] = [];

    for (const chunk of chunks) {
      results.push(...(await callGeminiForChunk(apiKey, model, chunk)));
    }

    return NextResponse.json({
      enabled: true,
      provider: "gemini",
      model,
      results,
    });
  } catch (error) {
    return NextResponse.json({
      enabled: false,
      provider: "deterministic",
      fallbackReason:
        error instanceof Error ? error.message : "gemini_definition_failed",
      results: [],
    });
  }
}
