import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
}

const ALLOWED_EXTENSIONS = [".csv", ".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".txt"];
const MAX_FILE_BYTES = 100 * 1024 * 1024; // 100 MB per file
const MAX_TOTAL_BYTES = 500 * 1024 * 1024; // 500 MB per import

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function parseBackendErrorPayload(payloadText: string) {
  if (!payloadText.trim()) {
    return null;
  }

  try {
    const payload = JSON.parse(payloadText) as { detail?: string; error?: string };
    return payload.detail ?? payload.error ?? null;
  } catch {
    return payloadText.trim();
  }
}

export async function POST(request: NextRequest) {
  const incomingFormData = await request.formData();
  const forwardedFormData = new FormData();

  let totalBytes = 0;
  for (const [key, value] of incomingFormData.entries()) {
    if (value instanceof File) {
      const ext = extensionOf(value.name);
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        return NextResponse.json(
          { error: `Unsupported file type "${ext || value.name}". Allowed: ${ALLOWED_EXTENSIONS.join(", ")}` },
          { status: 415 },
        );
      }
      if (value.size > MAX_FILE_BYTES) {
        return NextResponse.json(
          { error: `"${value.name}" exceeds the ${MAX_FILE_BYTES / (1024 * 1024)} MB per-file limit.` },
          { status: 413 },
        );
      }
      totalBytes += value.size;
      if (totalBytes > MAX_TOTAL_BYTES) {
        return NextResponse.json(
          { error: `Total upload exceeds the ${MAX_TOTAL_BYTES / (1024 * 1024)} MB limit.` },
          { status: 413 },
        );
      }
      forwardedFormData.append(key, value, value.name);
    } else {
      forwardedFormData.append(key, String(value));
    }
  }

  try {
    const response = await fetch(`${getBackendBaseUrl()}/runs/upload`, {
      method: "POST",
      body: forwardedFormData,
    });

    const text = await response.text();

    if (!response.ok) {
      return NextResponse.json(
        {
          error:
            parseBackendErrorPayload(text) ??
            `The Python backend returned ${response.status} while processing this import.`,
        },
        { status: response.status },
      );
    }

    return new NextResponse(text, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") ?? "application/json",
      },
    });
  } catch (error) {
    return NextResponse.json(
      {
        error:
          error instanceof Error
            ? error.message
            : "Unable to reach the local Python pipeline. Start `angelic-api` and try again.",
      },
      { status: 502 },
    );
  }
}
