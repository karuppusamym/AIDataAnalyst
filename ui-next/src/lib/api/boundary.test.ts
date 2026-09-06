import { describe, expect, it } from "vitest";

import * as barrel from "../api";
import * as administration from "./administration";
import * as agents from "./agents";
import * as catalog from "./catalog";
import * as columnDocumentation from "./columnDocumentation";
import * as crossSource from "./crossSource";
import * as glossary from "./glossary";
import * as governance from "./governance";
import * as identity from "./identity";
import * as lineage from "./lineage";
import * as operations from "./operations";
import * as products from "./products";
import * as quality from "./quality";
import * as semantics from "./semantics";
import * as sources from "./sources";
import * as studio from "./studio";
import * as tools from "./tools";
import * as transformations from "./transformations";

/* ---------------------------------------------------------------------------
   The two properties that make splitting this client worth anything (R05).

   1. ONE TRANSPORT. A domain module may not call `fetch` and may not assemble
      an identity header. Both rules exist because they were already broken:
      three modules had copied a transport, so the development-header rule
      (F06) never reached their endpoints and their `{"detail": ...}`-only
      decoders threw away the correlation id, error code and field errors F14
      preserves. A copy is easy to write and invisible in review, so it is
      asserted here rather than trusted.

   2. THE BARREL IS THE SURFACE. Screens import from `../lib/api` and must get
      exactly the function the domain module declares. A name declared in two
      modules does NOT fail a build once both are star-exported -- one wins
      silently -- which is how `decideRelationshipCandidate` ended up with two
      implementations of the same endpoint, only one of them reachable.
--------------------------------------------------------------------------- */

/** The transport is where the client's single `fetch` lives, by definition. */
const TRANSPORT = "./transport.ts";

/* Read through Vite rather than `node:fs`: `@types/node` is deliberately not
 * a dependency of this browser-only project (see `vite.config.ts`), and a
 * source-level assertion is not a reason to widen its type surface. */
const SOURCES: Record<string, string> = import.meta.glob("./*.ts", {
  query: "?raw",
  import: "default",
  eager: true,
});

function domainSources(): { name: string; text: string }[] {
  return Object.entries(SOURCES)
    .filter(([name]) => !name.endsWith(".test.ts") && name !== TRANSPORT)
    .map(([name, text]) => ({ name, text }));
}

/** Comments talk about headers; only code may not build them. */
function withoutComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^[ \t]*\/\/.*$/gm, "");
}

describe("one transport", () => {
  it("no domain module calls fetch", () => {
    const sources = domainSources();
    // An empty glob would make every assertion below vacuously true.
    expect(sources.length).toBeGreaterThan(10);
    const offenders = sources
      .filter(({ text }) => /\bfetch\s*\(/.test(withoutComments(text)))
      .map(({ name }) => name);
    expect(offenders).toEqual([]);
  });

  it("no domain module assembles an identity or authorization header", () => {
    const offenders = domainSources()
      .filter(({ text }) => {
        const code = withoutComments(text);
        return (
          /["']X-(Principal-Id|Roles|Organization-Id)["']/.test(code) ||
          /\bAuthorization\b\s*:/.test(code) ||
          /from ["']\.\.\/authSession["']/.test(code) ||
          /\bidentityHeaders\b/.test(code)
        );
      })
      .map(({ name }) => name);
    expect(offenders).toEqual([]);
  });

  it("the transport module is the only place that calls fetch", () => {
    const transport = SOURCES[TRANSPORT] ?? "";
    // One raw-body upload, because `http.ts`'s `request` JSON-stringifies its
    // body and cannot carry a `File`. If this count grows, a second transport
    // is being written -- add the verb to `http.ts` instead.
    expect(withoutComments(transport).match(/\bfetch\s*\(/g) ?? []).toHaveLength(1);
  });
});

describe("the barrel is the public surface", () => {
  const modules: Record<string, Record<string, unknown>> = {
    administration,
    agents,
    catalog,
    columnDocumentation,
    crossSource,
    glossary,
    governance,
    identity,
    lineage,
    operations,
    products,
    quality,
    semantics,
    sources,
    studio,
    tools,
    transformations,
  };

  /* `crossSource` and `governance` both declare `decideRelationshipCandidate`
   * against the same endpoint with different signatures. The barrel binds the
   * governed one, which is what `../lib/api` bound before the split; the other
   * stays reachable through `./_cross_source_api`. Listed rather than resolved
   * because deciding which client is correct is a change to behaviour, not to
   * file layout. */
  const KNOWN_COLLISIONS = new Set(["decideRelationshipCandidate"]);

  it("re-exports every domain module's runtime exports", () => {
    const missing: string[] = [];
    for (const [moduleName, mod] of Object.entries(modules)) {
      for (const name of Object.keys(mod)) {
        if (!(name in barrel)) missing.push(`${moduleName}.${name}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("binds each name to its declaring module, so nothing is silently shadowed", () => {
    const shadowed: string[] = [];
    for (const [moduleName, mod] of Object.entries(modules)) {
      for (const [name, value] of Object.entries(mod)) {
        if (KNOWN_COLLISIONS.has(name)) continue;
        if ((barrel as Record<string, unknown>)[name] !== value) {
          shadowed.push(`${moduleName}.${name}`);
        }
      }
    }
    expect(shadowed).toEqual([]);
  });
});
