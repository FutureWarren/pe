import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
}

export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ runId: string; artifactKey: string }> },
) {
  const { runId, artifactKey } = await context.params;

  try {
    const response = await fetch(
      `${getBackendBaseUrl()}/runs/${runId}/artifacts/${artifactKey}`,
    );

    return new NextResponse(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") ?? "application/octet-stream",
        "Content-Disposition":
          response.headers.get("content-disposition") ?? `attachment; filename="${artifactKey}"`,
      },
    });
  } catch (error) {
    return NextResponse.json(
      {
        error:
          error instanceof Error
            ? error.message
            : "Unable to download the backend artifact.",
      },
      { status: 502 },
    );
  }
}
