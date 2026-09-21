import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveAppConfig } from "./appConfig";

/* ---------------------------------------------------------------------------
   Development-only defaults for a UI that runs as one user (`scripts/demo-users.ps1`).

   `VITE_DEV_PERSONA` picks the persona the shell starts in, so the Auditor's UI opens as the Auditor
   rather than the shell's own default. `VITE_DEV_ORG_ID` picks the organization a browser with
   nothing remembered starts in, for a user who cannot list organizations and so has no picker.
   Neither is trusted blindly: a persona outside the five is dropped, and so is an id that is not a
   UUID, because a wrong tenant id sends every request to an organization that does not exist.
--------------------------------------------------------------------------- */

const ORG = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87";

describe("resolveAppConfig development defaults", () => {
  it("reads a persona and an organization from the environment", () => {
    const config = resolveAppConfig({
      VITE_USE_FIXTURES: "0",
      VITE_DEV_PERSONA: "Auditor",
      VITE_DEV_ORG_ID: ORG.toUpperCase(),
    });
    expect(config.devPersona).toBe("Auditor");
    expect(config.devOrgId).toBe(ORG);
  });

  it("is null when nothing is set, so the shell keeps its own defaults", () => {
    const config = resolveAppConfig({ VITE_USE_FIXTURES: "0" });
    expect(config.devPersona).toBeNull();
    expect(config.devOrgId).toBeNull();
  });

  it("drops a persona that is not one of the five and an id that is not a UUID", () => {
    const config = resolveAppConfig({
      VITE_USE_FIXTURES: "0",
      VITE_DEV_PERSONA: "Administrator",
      VITE_DEV_ORG_ID: "northwind",
    });
    expect(config.devPersona).toBeNull();
    expect(config.devOrgId).toBeNull();
  });
});

describe("the organization a browser starts in", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    localStorage.clear();
  });

  async function readInitialOrg(env: Record<string, string>): Promise<string> {
    vi.resetModules();
    for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value);
    const { readStoredOrgId } = await import("./org-context");
    return readStoredOrgId();
  }

  it("is the configured organization in a development build with nothing remembered", async () => {
    expect(await readInitialOrg({ VITE_USE_FIXTURES: "0", VITE_DEV_ORG_ID: ORG })).toBe(ORG);
  });

  it("is still whatever the browser remembered", async () => {
    localStorage.setItem("atlas.org.id", "5ee85f6d-9c27-4d87-93ad-dbe22acac062");
    expect(await readInitialOrg({ VITE_USE_FIXTURES: "0", VITE_DEV_ORG_ID: ORG })).toBe(
      "5ee85f6d-9c27-4d87-93ad-dbe22acac062",
    );
  });

  it("ignores the setting outside development identity", async () => {
    const id = await readInitialOrg({
      VITE_USE_FIXTURES: "0",
      VITE_AUTH_MODE: "oidc",
      VITE_DEV_ORG_ID: ORG,
    });
    expect(id).toBe("00000000-0000-0000-0000-000000000001");
  });
});
