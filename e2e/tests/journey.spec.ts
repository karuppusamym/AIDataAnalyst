/* ---------------------------------------------------------------------------
   The six journey steps, each performed by an identity holding ONLY the roles
   that step needs (tracker R11-B11).

   The seating is not decorative. A PlatformAdmin doing all six would prove
   nothing about authorization: every call would succeed whether or not the
   application had any role model at all. Each `test.use({ identity })` below
   names a seat whose role set is the one the application's own
   `require_roles` dependency demands for that step and nothing more -- the
   tuples are copied into `scripts/journey_stub_api.py` from
   `Docs/50-security/surface-control-matrix.md`, and
   `tests/test_journey_stub_contract.py` fails if they drift.

   Every step asserts two different things:
     - the request reached the API **through the production proxy**
       (`api.waitForCall`, which checks the upstream marker header that an
       SPA-fallback `index.html` cannot carry), and
     - the screen rendered the outcome, so a 200 that the UI ignores still
       fails.
--------------------------------------------------------------------------- */

import { expect, expectScreen, test } from "../support/journey";

test.describe("1. connect a source -- DataAdmin", () => {
  test.use({ identity: "connector" });

  test("registers a datasource through the proxy", async ({ page, api }) => {
    await page.goto("/#/administration");
    await expectScreen(page, "administration");

    const form = page.locator('form[aria-label="Register data source"]');
    await expect(form).toBeVisible();

    await form.getByLabel("Project").selectOption({ index: 1 });
    await form.getByLabel("Source name").fill("Journey warehouse");
    await form.getByLabel("Credential reference").fill("env://AIDA_SAMPLE_SOURCE_DSN");
    await form.getByRole("button", { name: "Register source" }).click();

    await api.waitForCall("POST", /^\/v1\/projects\/[^/]+\/datasources$/, 200);
    // The screen's own acknowledgement. Without this the test would pass on a
    // request the UI fired and then discarded.
    await expect(form.getByText('Registered "Journey warehouse".')).toBeVisible();
  });
});

test.describe("2. scan the source -- DataAdmin", () => {
  test.use({ identity: "connector" });

  test("starts the first scan through the proxy", async ({ page, api }) => {
    // Scanning is the one step that changes the stub's state, so put it back
    // to "never scanned" first. Re-running the suite against an already-used
    // container must give the same result as the first run.
    await page.request.post("/v1/__journey/reset");
    await page.goto("/#/home");
    await expectScreen(page, "home");

    const setup = page.locator('section[aria-label="First source setup"]');
    await expect(setup.getByRole("heading", { name: "Make a source usable" })).toBeVisible();

    await setup.getByRole("button", { name: "Start the first scan" }).click();

    await api.waitForCall("POST", /^\/v1\/datasources\/[^/]+\/analysis-runs$/, 200);
    // The stub reports a QUEUED run once the POST has landed, so the step must
    // leave "not started" -- which is the only way to tell the click through
    // from a button that merely re-rendered.
    await expect(setup.getByRole("button", { name: "Start the first scan" })).toHaveCount(0);
  });
});

test.describe("3. describe an asset -- DataSteward", () => {
  test.use({ identity: "steward" });

  test("submits a description draft for review through the proxy", async ({ page, api }) => {
    await page.goto("/#/description-drafts");
    await expectScreen(page, "description-drafts");

    const list = page.getByRole("list", { name: "Description drafts" });
    await expect(list.getByText("public.orders")).toBeVisible();

    await page.getByRole("button", { name: "Submit for review" }).click();

    await api.waitForCall("POST", /^\/v1\/asset-description-drafts\/[^/]+\/submit$/, 200);
    // Scoped to the list: the status FILTER also contains the words "Pending
    // approval", and an unscoped match would pass on the control rather than
    // on the row that changed.
    await expect(list.getByText("pending approval", { exact: true })).toBeVisible();
  });
});

test.describe("4. review the proposal -- Reviewer", () => {
  test.use({ identity: "reviewer" });

  test("approves a governance review through the proxy", async ({ page, api }) => {
    await page.goto("/#/governance");
    await expectScreen(page, "governance");

    const queue = page.getByRole("list", { name: "Governance review queue" });
    // The row's own status pill. ("pending review" is the summary TILE's
    // label, which is present even when the queue is empty.)
    await expect(queue.getByText("review needed", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Approve" }).first().click();

    await api.waitForCall("POST", /^\/v1\/governance\/reviews\/[^/]+\/decision$/, 200);
    await expect(page.getByText("Approval recorded.")).toBeVisible();
  });
});

test.describe("5. Ask -- Analyst", () => {
  test.use({ identity: "analyst" });

  test("asks a governed question through the proxy", async ({ page, api }) => {
    await page.goto("/#/analyst");
    await expectScreen(page, "analyst");

    await page.getByLabel("Datasource").selectOption({ label: "Journey warehouse" });
    // By role: the history list is also labelled "Past questions", which a
    // bare `getByLabel("Question")` matches too.
    await page
      .getByRole("textbox", { name: "Question" })
      .fill("what was net revenue by month last quarter?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();

    await api.waitForCall("POST", /^\/v1\/datasources\/[^/]+\/agent-analyses$/, 200);
    await expect(page.getByLabel(/^Answer for run /)).toBeVisible();
  });
});

test.describe("6. evidence -- Auditor", () => {
  test.use({ identity: "auditor" });

  test("reads the audit ledger through the proxy", async ({ page, api }) => {
    await page.goto("/#/audit");
    await expectScreen(page, "audit");

    await api.waitForCall("GET", /^\/v1\/organizations\/[^/]+\/audit-events$/, 200);

    const events = page.getByRole("list", { name: "Audit events" });
    await expect(events.getByRole("article", { name: "governance_review.decide" })).toBeVisible();

    // The evidence itself: open the event and confirm the record carries what
    // an auditor needs to follow it -- not merely that a row exists.
    await events.getByRole("article", { name: "governance_review.decide" }).click();
    const detail = page.locator('aside[aria-label^="Event "]');
    await expect(detail).toBeVisible();
    await expect(detail.getByText("journey-stub-correlation")).toBeVisible();
  });
});
