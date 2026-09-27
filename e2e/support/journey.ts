/* ---------------------------------------------------------------------------
   Fixtures for the browser journey suite (tracker R11-B11).

   TWO THINGS LIVE HERE.

   1. THE IDENTITY SEAT. The SPA under test is built with
      `VITE_AUTH_MODE=proxy`, a mode the application already supports: the
      browser asserts no identity of its own because an authenticating reverse
      proxy in front of it is the authority. `scripts/journey_stub_api.py`
      plays that authority and reads the identity from a cookie, so a test
      declares who it is with `test.use({ identity: "steward" })` and every
      request it makes is authorized as that principal and no other.

      Why a cookie and not a header: `VITE_DEV_PRINCIPAL_ID` / `VITE_DEV_ROLES`
      are `import.meta.env` values baked in by `vite build`, so seating six
      identities through the browser's own headers would mean six images of
      the thing we are trying to test once.

   2. THE UPSTREAM RECORDER. `X-Atlas-Stub-Upstream` is set by the stub on
      every response it serves and survives the proxy hop unchanged. An
      `index.html` returned by nginx's SPA fallback cannot carry it. So
      `api.waitForCall(...)` asserting on that header is what proves a
      journey step's request was *routed to the API* rather than silently
      answered with the SPA shell -- the F07 defect class, observed from
      inside the browser rather than from curl.
--------------------------------------------------------------------------- */

import { test as base, expect } from "@playwright/test";

export const IDENTITY_COOKIE = "atlas_journey_identity";
export const UPSTREAM_HEADER = "x-atlas-stub-upstream";

/** The least-privilege seats. Must match `IDENTITIES` in the stub. */
export type Identity =
  | "connector"
  | "steward"
  | "reviewer"
  | "analyst"
  | "auditor"
  | "bystander";

export interface ApiCall {
  readonly method: string;
  readonly path: string;
  readonly status: number;
  /** True when the response carried the stub's marker header. */
  readonly upstream: boolean;
}

export class ApiRecorder {
  readonly calls: ApiCall[] = [];

  find(method: string, path: RegExp): ApiCall | undefined {
    return this.calls.find((c) => c.method === method && path.test(c.path));
  }

  /**
   * Wait for a call to appear, then assert it reached the API through the
   * proxy with `expectedStatus`.
   *
   * `expect.poll` rather than a sleep: it retries the predicate on
   * Playwright's own schedule and fails with the recorded calls in the
   * message, so a failure names what DID happen instead of timing out blind.
   */
  async waitForCall(method: string, path: RegExp, expectedStatus: number): Promise<ApiCall> {
    await expect
      .poll(() => this.find(method, path)?.status, {
        message: `no ${method} matching ${path} was observed; saw:\n${this.describe()}`,
        timeout: 15_000,
      })
      .toBe(expectedStatus);
    const call = this.find(method, path)!;
    expect(
      call.upstream,
      `${method} ${call.path} did not carry ${UPSTREAM_HEADER}: it was answered by the SPA ` +
        `fallback rather than routed to the API`,
    ).toBe(true);
    return call;
  }

  describe(): string {
    return this.calls.map((c) => `  ${c.status} ${c.method} ${c.path}`).join("\n") || "  (none)";
  }
}

export const test = base.extend<{ identity: Identity; api: ApiRecorder }>({
  // Overridable per file with `test.use({ identity: "..." })`. The default is
  // the seat that holds nothing but `Viewer`, so a test that forgets to
  // declare one gets the least privilege rather than the most.
  identity: ["bystander", { option: true }],

  context: async ({ context, identity, baseURL }, use) => {
    await context.addCookies([
      { name: IDENTITY_COOKIE, value: identity, url: baseURL ?? "http://localhost:8099" },
    ]);
    await use(context);
  },

  api: async ({ context }, use) => {
    const recorder = new ApiRecorder();
    // Registered on the context, not the page, so a reload or a second page
    // keeps recording -- the reload case depends on that.
    context.on("response", (response) => {
      let path: string;
      try {
        path = new URL(response.url()).pathname;
      } catch {
        return;
      }
      if (!path.startsWith("/v1/") && !path.startsWith("/mcp")) return;
      recorder.calls.push({
        method: response.request().method(),
        path,
        status: response.status(),
        upstream: response.headers()[UPSTREAM_HEADER] === "journey",
      });
    });
    await use(recorder);
  },
});

export { expect };

/** The screen id the shell currently has mounted (`<div class="sview">`). */
export function mountedScreen(page: import("@playwright/test").Page) {
  return page.locator(".sview");
}

/**
 * Assert the app is alive and showing `screenId`.
 *
 * Deliberately more than a URL check: the three cross-cutting cases are all
 * about the difference between "the address bar says the right thing" and
 * "the application actually rendered".
 */
export async function expectScreen(
  page: import("@playwright/test").Page,
  screenId: string,
): Promise<void> {
  await expect(mountedScreen(page)).toHaveAttribute("data-screen", screenId);
  // A route that threw renders the boundary instead of the screen, which
  // would otherwise satisfy the attribute check above.
  await expect(page.getByTestId("route-error")).toHaveCount(0);
}
