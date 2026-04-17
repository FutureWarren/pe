import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

function getBackendBaseUrl() {
  return process.env.ANGELIC_API_BASE_URL ?? "http://127.0.0.1:8011";
}

export async function GET(request: NextRequest) {
  const limit = request.nextUrl.searchParams.get("limit") ?? "200";

  try {
    const response = await fetch(`${getBackendBaseUrl()}/runs?limit=${encodeURIComponent(limit)}`);
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
