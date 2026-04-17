import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
}

export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ runId: string }> },
) {
  const { runId } = await context.params;

  try {
    const response = await fetch(`${getBackendBaseUrl()}/runs/${encodeURIComponent(runId)}/payload`);
    const text = await response.text();

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
            : "Unable to reach the local Python API. Start `angelic-api` and try again.",
      },
      { status: 502 },
    );
  }
}
