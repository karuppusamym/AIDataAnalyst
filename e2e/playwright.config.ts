/* ---------------------------------------------------------------------------
   Browser journey suite -- configuration (tracker R11-B11).

   WHAT THIS SUITE RUNS AGAINST, and why it matters more than anything else
   in this file: the REAL `ui-next` production image -- nginx 1.27 serving the
   real built SPA with the real `ui-next/nginx.conf` -- not the Vite dev
   server. The two differ in exactly the ways that break a deployment:

     - base paths and asset URLs (`vite build` output vs dev module graph),
     - SPA fallback (`try_files $uri $uri/ /index.html`) for a pasted URL,
     - which prefixes are proxied to the API (`/v1/`, `/mcp`) and which fall
       through to the SPA -- the F07 defect class,
     - header forwarding across the proxy hop.

   Testing the dev server would test the thing that is not shipped.

   RUNNING IT LOCALLY. The `ui-journey` job in `.github/workflows/ci.yml` is
   the authority; these are the same commands, from the repository root:

     docker build --build-arg VITE_USE_FIXTURES=0 --build-arg VITE_AUTH_MODE=proxy \
       --tag ui-next-journey:local ./ui-next
     docker network create atlas-journey-ci
     docker run -d --name atlas-journey-stub --network atlas-journey-ci \
       --network-alias api -v "$PWD:/srv:ro" \
       python:3.13-slim python /srv/scripts/journey_stub_api.py 8000
     docker run -d --name atlas-journey-ui --network atlas-journey-ci \
       -p 8099:80 ui-next-journey:local

   Then wait until the proxy actually reaches the upstream -- `/health` is
   answered by nginx itself and proves nothing about the API path:

     curl -fsS -o /dev/null -H 'Cookie: atlas_journey_identity=auditor' -D - \
       http://localhost:8099/v1/me | grep -i '^x-atlas-stub-upstream: journey'

     cd e2e && npm ci && npx playwright install chromium && npm test

   Tear down with `docker rm -f atlas-journey-ui atlas-journey-stub` and
   `docker network rm atlas-journey-ci`.

   RETRIES ARE ZERO, DELIBERATELY. A browser job that is retried until green
   protects nothing: it converts a real intermittent defect into a slower
   build. Every wait in this suite is on an application-visible condition
   (Playwright's auto-waiting `expect` assertions), never on a sleep, so a
   failure here is a fact about the build rather than about the runner's
   scheduling. If a case cannot be made deterministic it belongs out of the
   suite, not behind a retry.
--------------------------------------------------------------------------- */

import { defineConfig, devices } from "@playwright/test";

/** Where the production proxy is published. `run-journey.sh` sets this. */
const baseURL = process.env.ATLAS_E2E_BASE_URL ?? "http://localhost:8099";

export default defineConfig({
  testDir: "./tests",
  /* The whole point. See the header. */
  retries: 0,
  /* A `test.only` left in a commit would silently shrink the suite to one
     case while still reporting green. */
  forbidOnly: !!process.env.CI,
  /* One worker: the stub upstream keeps per-identity state (the datasource a
     test connects, the drafts it submits), and serialising removes any
     question of one test observing another's writes. The suite is small
     enough that this costs seconds. */
  workers: 1,
  fullyParallel: false,
  /* A hung page must fail the job, not hang the runner until GitHub's own
     6-hour ceiling. */
  timeout: 60_000,
  globalTimeout: 10 * 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]]
    : [["list"]],
  use: {
    baseURL,
    /* Retained only for a failing test, so a green run uploads nothing and a
       red one carries everything needed to see why. */
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
