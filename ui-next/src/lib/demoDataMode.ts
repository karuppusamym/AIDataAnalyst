/* ---------------------------------------------------------------------------
   Whether a build carries demo data at all (review 2026-09-11, R11-X1).

   `lib/fixtures.ts` is the bundled demo estate -- roughly a third of all the
   JavaScript this client shipped. It was reachable in every build because the
   demo/live decision was read at RUNTIME (`VITE_USE_FIXTURES !== "0"` against
   a live `import.meta.env`), and a bundler cannot drop a branch whose
   condition it only learns about in the browser. So production users
   downloaded the whole demo estate to run a code path they could never take.

   This function is the decision, made once, in Node, while the build is being
   configured. `vite.config.ts` turns its answer into a literal
   `import.meta.env.VITE_USE_FIXTURES` (see the `define` there), which is what
   lets Rollup fold the guard in `api/transport.ts` and drop the fixture module
   from the graph entirely.

   It lives here rather than inline in `vite.config.ts` because the rule is
   worth pinning in a test: getting it wrong silently ships the demo estate
   again, or -- worse -- silently serves demo data to a production user.
--------------------------------------------------------------------------- */

/** The environment keys this decision reads. */
export interface DemoDataEnv {
  readonly VITE_USE_FIXTURES?: string | undefined;
}

/**
 * True when this build should carry, and answer from, the bundled fixtures.
 *
 * An explicitly set `VITE_USE_FIXTURES` always wins: the Docker image passes
 * `0` and the development Compose service can pass `1`, and neither should
 * have to know what Vite calls the current mode. Absent that, the mode
 * decides -- `development` (the dev server) and `demo` (a deliberately built
 * demo site, `vite build --mode demo`) carry fixtures; everything else,
 * `production` above all, does not.
 *
 * Note that "unset" is the case that changed: it used to mean demo data, in
 * every mode. A production build now has to ask for the demo estate by name.
 */
export function resolveDemoData(mode: string, env: DemoDataEnv): boolean {
  const declared = env.VITE_USE_FIXTURES;
  if (declared !== undefined && declared !== "") return declared !== "0";
  return mode === "development" || mode === "demo";
}
