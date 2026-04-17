import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
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

  for (const [key, value] of incomingFormData.entries()) {
    if (value instanceof File) {
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
