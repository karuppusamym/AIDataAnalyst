import { describe, expect, it } from "vitest";
import { resolveAppConfig } from "../lib/appConfig";
import {
  ADDIN_AUTH_PATH,
  dialogAuthConfig,
  formatDialogMessage,
  parseDialogMessage,
} from "./authMessage";
import { readWorkbookIdentity } from "./workbookIdentity";

describe("readWorkbookIdentity", () => {
  it("reads the rows the server's export writes", () => {
    const identity = readWorkbookIdentity([
      ["Field", "Value"],
      ["Datasource", "warehouse"],
      ["Datasource id", "11111111-2222-3333-4444-555555555555"],
      ["Organization id", "00000000-0000-0000-0000-000000000009"],
    ]);
    expect(identity).toEqual({
      datasourceId: "11111111-2222-3333-4444-555555555555",
      organizationId: "00000000-0000-0000-0000-000000000009",
      datasourceName: "warehouse",
    });
  });

  it("treats a mangled id as no identity at all, never as a different one", () => {
    const identity = readWorkbookIdentity([["Datasource id", "11111111-2222-3333-4444-55555555"]]);
    expect(identity.datasourceId).toBeNull();
  });

  it("has no identity when there is no README", () => {
    expect(readWorkbookIdentity(null).datasourceId).toBeNull();
  });
});

describe("the sign-in dialog's message", () => {
  it("redirects the dialog back to its own page, not the shell's", () => {
    const config = resolveAppConfig({
      VITE_AUTH_MODE: "oidc",
      VITE_OIDC_ISSUER: "https://idp.example",
      VITE_OIDC_CLIENT_ID: "atlas-ui",
    });
    expect(dialogAuthConfig(config)?.oidc?.redirectPath).toBe(ADDIN_AUTH_PATH);
    expect(dialogAuthConfig(resolveAppConfig({}))).toBeNull();
  });

  it("round-trips a token, and never adopts anything malformed as one", () => {
    const sent = formatDialogMessage({ kind: "token", token: "abc", expiresInSeconds: 300 });
    expect(parseDialogMessage(sent)).toEqual({ kind: "token", token: "abc", expiresInSeconds: 300 });
    expect(parseDialogMessage("not json").kind).toBe("error");
    expect(parseDialogMessage(JSON.stringify({ kind: "token", token: "" })).kind).toBe("error");
    expect(parseDialogMessage(JSON.stringify({ token: "abc" })).kind).toBe("error");
  });
});
