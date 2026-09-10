import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mcpEndpoint, runGatewayDiagnostic } from "./AgentGatewayConnect";

/* ---------------------------------------------------------------------------
   The connection diagnostic (review 2026-09-05, F07).

   THE DEFECT: the screen advertised `${origin}/mcp` and had no idea whether
   that URL reaches anything. Vite proxies `/mcp` in development, so it worked
   wherever the screen was written; the production Nginx proxies only `/v1/`,
   so `/mcp` fell through to the SPA's history fallback and answered with
   `index.html` -- HTML, HTTP 200, no JSON-RPC. The engineer found out in
   their own terminal an hour later.

   The distinction that matters is between three outcomes with three different
   repairs, in three different places: the proxy is misconfigured, the
   credentials are wrong, or nothing is listening. A single green/red light
   would collapse all three.
--------------------------------------------------------------------------- */

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function response(body: string, init: { status?: number; contentType?: string } = {}) {
  return {
    status: init.status ?? 200,
    headers: { get: () => init.contentType ?? "application/json" },
    text: () => Promise.resolve(body),
    json: () => Promise.resolve(JSON.parse(body)),
  } as unknown as Response;
}

describe("runGatewayDiagnostic", () => {
  it("calls the exact URL the screen tells the user to copy", async () => {
    fetchMock.mockResolvedValue(response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} })));

    await runGatewayDiagnostic("org1");

    expect(fetchMock.mock.calls[0]![0]).toBe(mcpEndpoint());
    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(init.method).toBe("POST");
    // Read-only: `ping` returns `{}` and executes nothing.
    expect(String(init.body)).toContain('"method":"ping"');
  });

  it("names the SPA shell when the proxy did not route /mcp to the API", async () => {
    fetchMock.mockResolvedValue(
      response("<!doctype html><html><body>Atlas</body></html>", { contentType: "text/html" }),
    );

    const result = await runGatewayDiagnostic("org1");

    expect(result.outcome).toBe("spa-shell");
    expect(result.detail).toMatch(/not routing \/mcp to the API/);
    expect(result.detail).toMatch(/Add the \/mcp upstream/);
  });

  it("separates a refused credential from a broken topology", async () => {
    fetchMock.mockResolvedValue(response(JSON.stringify({ detail: "unauthorized" }), { status: 401 }));

    const result = await runGatewayDiagnostic("org1");

    expect(result.outcome).toBe("unauthorized");
    expect(result.detail).toMatch(/topology is correct/);
  });

  it("reports nothing answering as its own outcome", async () => {
    fetchMock.mockRejectedValue(new Error("Failed to fetch"));

    const result = await runGatewayDiagnostic("org1");

    expect(result.outcome).toBe("unreachable");
    expect(result.status).toBeNull();
  });

  it("reports how many tools this caller would actually be shown", async () => {
    fetchMock
      .mockResolvedValueOnce(response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} })))
      .mockResolvedValueOnce(
        response(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 2,
            result: { tools: [{ name: "a" }, { name: "b" }] },
          }),
        ),
      );

    const result = await runGatewayDiagnostic("org1");

    expect(result.outcome).toBe("reached");
    expect(result.toolCount).toBe(2);
    expect(String(fetchMock.mock.calls[1]![1] && (fetchMock.mock.calls[1]![1] as RequestInit).body))
      .toContain('"method":"tools/list"');
  });

  it("reports an unreadable tool list as unknown rather than as zero", async () => {
    fetchMock
      .mockResolvedValueOnce(response(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} })))
      .mockRejectedValueOnce(new Error("boom"));

    const result = await runGatewayDiagnostic("org1");

    expect(result.outcome).toBe("reached");
    // Zero would read as "you are permitted nothing", which is a different
    // and much more alarming claim than "we could not tell".
    expect(result.toolCount).toBeNull();
    expect(result.detail).toMatch(/visible tool count is unknown/);
  });
});
