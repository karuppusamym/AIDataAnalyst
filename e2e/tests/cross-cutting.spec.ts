/* ---------------------------------------------------------------------------
   The three cases the tracker row calls out as the ones that actually regress
   (R11-B11): reload, deep links, denied access.

   All three are properties of the PRODUCTION topology specifically, which is
   why this suite runs against the real nginx image rather than the Vite dev
   server:

     - reload and deep links depend on `try_files $uri $uri/ /index.html` in
       `ui-next/nginx.conf`. The dev server has its own, different fallback,
       so a suite run against it cannot fail when the shipped one is wrong.
     - denied access depends on the refusal surviving the proxy hop with its
       status and body intact.
--------------------------------------------------------------------------- */

import { expect, expectScreen, test } from "../support/journey";

/* --- reload ------------------------------------------------------------- */

test.describe("reload mid-journey", () => {
  test.use({ identity: "reviewer" });

  test("keeps you where you were, and does not 404 at the proxy", async ({ page }) => {
    await page.goto("/#/governance");
    await expectScreen(page, "governance");

    // Get mid-journey: open one proposal, so there is a selection to lose.
    await page.getByRole("list", { name: "Governance review queue" }).getByRole("button").first().click();
    const detail = page.locator('[aria-label="Proposal detail"]');
    await expect(detail).toBeVisible();
    const before = new URL(page.url());
    expect(before.searchParams.get("review"), "the selection must be in the URL to survive").toBeTruthy();

    const response = await page.reload({ waitUntil: "domcontentloaded" });

    // THE PROXY ASSERTION. `?review=…#/governance` requests path `/`, but a
    // reload must be served the SPA rather than refused, and a 404 here is the
    // classic single-page-app deployment failure.
    expect(response?.status(), "the reload was not served by the proxy").toBe(200);

    // THE APPLICATION ASSERTION: same screen, same selection, still rendered.
    await expectScreen(page, "governance");
    expect(new URL(page.url()).searchParams.get("review")).toBe(before.searchParams.get("review"));
    await expect(page.locator('[aria-label="Proposal detail"]')).toBeVisible();
  });

  test("survives a reload on a nested path, which is where SPA fallback bites", async ({ page }) => {
    // Nothing is served at this path: only `try_files`'s `/index.html` arm can
    // answer it. This is the case that breaks when a deployment adds a
    // `location` block or drops the fallback.
    const response = await page.goto("/deep/nested/path#/governance");
    expect(response?.status()).toBe(200);
    await expectScreen(page, "governance");

    const reloaded = await page.reload({ waitUntil: "domcontentloaded" });
    expect(reloaded?.status()).toBe(200);
    await expectScreen(page, "governance");
  });
});

/* --- deep links --------------------------------------------------------- */

test.describe("deep links", () => {
  test.use({ identity: "auditor" });

  test("a pasted URL for a nested view loads it directly", async ({ page, api }) => {
    // The router is hash-based (`ui-next/src/lib/routes.ts` -> `screenFromHash`)
    // and the query string sits BEFORE the hash
    // (`ui-next/src/lib/location.ts` -> `commit`), so this is the exact shape
    // the app's own "Copy permalink" buttons produce.
    await page.goto("/?action=governance_review.decide#/audit");

    await expectScreen(page, "audit");
    await api.waitForCall("GET", /^\/v1\/organizations\/[^/]+\/audit-events$/, 200);

    // The deep link's query must reach the screen, not just the address bar:
    // the filter it names is applied.
    await expect(page.getByLabel("Action")).toHaveValue("governance_review.decide");
    await expect(
      page.getByRole("list", { name: "Audit events" }).getByRole("article").first(),
    ).toBeVisible();
  });

  test("the hash names the screen, so an unknown screen degrades instead of 404ing", async ({
    page,
  }) => {
    const response = await page.goto("/#/no-such-screen");
    expect(response?.status()).toBe(200);
    // `screenFromHash` resolves an unrecognised id to the default screen --
    // a stale bookmark lands somewhere real rather than on a blank page.
    await expectScreen(page, "home");
  });
});

/* --- denied access ------------------------------------------------------ */

test.describe("denied access", () => {
  test.describe("a read the identity may not perform", () => {
    // Viewer only. `GET /v1/organizations/{organization_id}/audit-events`
    // requires Auditor / Operations / OrganizationAdmin / PlatformAdmin.
    test.use({ identity: "bystander" });

    test("lands on a real refusal, not a blank screen or a forever spinner", async ({
      page,
      api,
    }) => {
      await page.goto("/#/audit");
      await expectScreen(page, "audit");

      const call = await api.waitForCall("GET", /^\/v1\/organizations\/[^/]+\/audit-events$/, 403);
      expect(call.status).toBe(403);

      // 1. THE REFUSAL IS ON THE PAGE. This is the assertion the case exists
      //    for: a 403 the UI swallows would leave the screen looking merely
      //    empty, which reads as "there is no audit history".
      const refusal = page.getByRole("alert").filter({
        hasText: "The audit ledger could not be loaded",
      });
      await expect(refusal).toBeVisible();

      // 2. IT SAYS WHY, carrying the server's own message across the proxy.
      await expect(refusal).toContainText("one of these roles is required");

      // 3. IT IS NOT AN EMPTY STATE. The empty and the denied renders are
      //    different components and must not be confused.
      await expect(page.getByText("No audit events match these filters")).toHaveCount(0);

      // 4. IT IS NOT A FOREVER SPINNER.
      await expect(page.getByText("Loading audit ledger…")).toHaveCount(0);

      // 5. IT IS NOT A BLANK SCREEN: the shell is still navigable, so the
      //    user can go somewhere they ARE entitled to.
      await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    });
  });

  test.describe("a write the identity may not perform", () => {
    // Reviewer may LIST description drafts but may not submit one:
    // `POST /v1/asset-description-drafts/{draft_id}/submit` requires
    // DataSteward / MetadataAdmin / PlatformAdmin / SemanticAdmin.
    test.use({ identity: "reviewer" });

    test("refuses the action visibly and does not fake success", async ({ page, api }) => {
      await page.goto("/#/description-drafts");
      await expectScreen(page, "description-drafts");

      const list = page.getByRole("list", { name: "Description drafts" });
      await expect(list.getByText("public.orders")).toBeVisible();

      await page.getByRole("button", { name: "Submit for review" }).click();

      await api.waitForCall("POST", /^\/v1\/asset-description-drafts\/[^/]+\/submit$/, 403);

      // The refusal is rendered against the row that was refused...
      await expect(list.getByRole("alert")).toContainText("one of these roles is required");
      // ...and the optimistic state change is rolled back, so the UI does not
      // report a submission the server rejected.
      await expect(list.getByText("pending approval", { exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Submit for review" })).toBeVisible();
    });
  });
});
