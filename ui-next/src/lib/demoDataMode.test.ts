import { describe, expect, it } from "vitest";
import { resolveDemoData } from "./demoDataMode";

/* ---------------------------------------------------------------------------
   R11-X1: demo data must not reach a production build.

   Two halves, and both are needed -- either one alone passes while the demo
   estate still ships.

     THE DECISION   -- a production build resolves to "no demo data", so
                       `vite.config.ts` defines the flag to a literal `"0"` and
                       the guard in `api/transport.ts` folds.
     THE GRAPH      -- nothing but that one guarded, dynamic `import()` refers
                       to `lib/fixtures.ts`. A single static `import` anywhere
                       in the client re-attaches ~189 kB to every entry point,
                       silently: the build still succeeds, the tests still
                       pass, and only a bundle listing would show it.

   The second half is asserted against the sources rather than a built bundle
   on purpose -- it is the property a reviewer can act on, it names the file
   that broke it, and it costs a test run rather than a `vite build`.
--------------------------------------------------------------------------- */

describe("resolveDemoData", () => {
  it("leaves demo data out of a production build", () => {
    expect(resolveDemoData("production", {})).toBe(false);
  });

  it("carries demo data for the dev server and for a deliberate demo build", () => {
    expect(resolveDemoData("development", {})).toBe(true);
    expect(resolveDemoData("demo", {})).toBe(true);
  });

  it("lets an explicit flag override the mode in either direction", () => {
    // The Docker image passes `0`; the Compose service can pass `1`. Neither
    // should have to know what Vite calls the mode it is building in.
    expect(resolveDemoData("production", { VITE_USE_FIXTURES: "1" })).toBe(true);
    expect(resolveDemoData("development", { VITE_USE_FIXTURES: "0" })).toBe(false);
  });

  it("treats an unset and an empty flag alike, rather than reading '' as live", () => {
    // Docker `ARG`/`ENV` pairs hand an unset variable through as an empty
    // string, which `!== "0"` would have read as "demo data, please".
    expect(resolveDemoData("production", { VITE_USE_FIXTURES: "" })).toBe(false);
    expect(resolveDemoData("development", { VITE_USE_FIXTURES: undefined })).toBe(true);
  });
});

const SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** A static `import ... from ".../fixtures"` -- the kind no build can drop. */
const STATIC_FIXTURE_IMPORT = /^\s*(?:import|export)\b[^;]*?from\s*"[^"]*\/fixtures";/m;

const isTest = (path: string) => /\.test\.tsx?$/.test(path);

/** `import.meta.glob` keys are spelled relative to this file; compare on the
 *  part that identifies the module rather than on that spelling. */
const TRANSPORT = "api/transport.ts";
const named = (path: string) => path.replace(/^[./]+/, "");

describe("the fixture module's reachability", () => {
  it("covers the whole client, so an offender cannot hide outside the glob", () => {
    const covered = Object.keys(SOURCES).map(named);
    expect(covered).toContain(TRANSPORT);
    expect(covered).toContain("fixtures.ts");
    expect(covered.some((path) => path.startsWith("screens/"))).toBe(true);
    expect(covered.some((path) => path.startsWith("components/"))).toBe(true);
    expect(covered.some((path) => path.startsWith("excelAddin/"))).toBe(true);
  });

  it("is imported statically by nothing but tests", () => {
    const offenders = Object.entries(SOURCES)
      .filter(([path]) => !isTest(path))
      .filter(([, source]) => STATIC_FIXTURE_IMPORT.test(source))
      .map(([path]) => named(path));

    // Tests may import fixtures freely -- they are never bundled. Anything
    // else here is a module that has just put the demo estate back into every
    // production bundle; route its demo answer through `demoOr` instead.
    expect(offenders).toEqual([]);
  });

  it("is reached only through the transport seam's dynamic import", () => {
    const dynamicImporters = Object.entries(SOURCES)
      .filter(([path]) => !isTest(path))
      .filter(([, source]) => /\bimport\(\s*"[^"]*\/fixtures"\s*\)/.test(source))
      .map(([path]) => named(path));

    expect(dynamicImporters).toEqual([TRANSPORT]);
  });

  it("folds its guard on `import.meta.env`, not on an imported constant", () => {
    // This is the load-bearing detail, and the easiest one to "tidy" away:
    // `appConfig` lands in a different chunk, and Rollup cannot fold a
    // constant it has to cross a chunk boundary to read. Swapping this
    // comparison for the equivalent `USE_FIXTURES` import leaves the dynamic
    // import live and ships the fixture chunk again -- with every test still
    // green, because in a test build demo data is on anyway.
    const transport =
      Object.entries(SOURCES).find(([path]) => named(path) === TRANSPORT)?.[1] ?? "";
    expect(transport).toMatch(/import\.meta\.env\.VITE_USE_FIXTURES === "0"/);
  });
});
