import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
}

// Run ids and artifact keys are simple slugs; reject anything with path
// separators or traversal so a crafted value cannot pivot the server-side fetch
// to an arbitrary backend path (SSRF / traversal).
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

export async function GET(
  _request: NextRequest,
  context: { params: Promise<{ runId: string; artifactKey: string }> },
) {
  const { runId, artifactKey } = await context.params;

  if (!SAFE_SEGMENT.test(runId) || !SAFE_SEGMENT.test(artifactKey)) {
    return NextResponse.json({ error: "Invalid run or artifact identifier." }, { status: 400 });
  }

  try {
    const response = await fetch(
      `${getBackendBaseUrl()}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactKey)}`,
    );

    if (!response.ok) {
      // Do not stream a non-2xx body as a "download" — the user would save an
      // error payload named like a workbook. Surface it as JSON instead.
      const text = await response.text().catch(() => "");
      return NextResponse.json(
        { error: text.trim() || `The backend returned ${response.status} for this artifact.` },
        { status: response.status },
      );
    }

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
