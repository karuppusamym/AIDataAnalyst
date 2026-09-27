import { describe, expect, it } from "vitest";

import { readDecision, roleAllows, roleHolds } from "./roles";
import type { SessionState } from "./session";
import type { MeRead } from "./types";

/* ---------------------------------------------------------------------------
   The three rules `lib/roles.ts` states, pinned as a truth table.

   `readDecision` exists because `roleAllows` -- which fails OPEN while identity is
   in flight -- was used to decide whether a screen should ASK, so a session that
   turned out not to be admitted sent a request the server had to refuse on every
   load (the demo rehearsal counted it for `sam.agentdev` on Home). A read is now
   HELD while the session is `connecting`, and asked for only once identity has
   answered, is admitted, or is known never to come.
--------------------------------------------------------------------------- */

const ACCEPTED = ["DataSteward", "Reviewer"];
const asMe = (...roles: string[]): MeRead => ({
  principal_id: "someone", principal_type: "USER", organization_id: null, roles,
  persona: null, identity_provider: "DEVELOPMENT",
});

describe("readDecision: a load-time read", () => {
  it("waits while identity is in flight, whatever `me` holds", () => {
    expect(readDecision({ state: "connecting", me: null }, ACCEPTED)).toBe("wait");
    // A stale `me` from a previous identity must not be trusted while a new one is being resolved.
    expect(readDecision({ state: "connecting", me: asMe("Viewer") }, ACCEPTED)).toBe("wait");
  });

  it("asks when a role is admitted, and skips when none is", () => {
    expect(readDecision({ state: "connected", me: asMe("Reviewer") }, ACCEPTED)).toBe("ask");
    expect(readDecision({ state: "connected", me: asMe("Viewer", "DataSteward") }, ACCEPTED)).toBe("ask");
    expect(readDecision({ state: "connected", me: asMe("Viewer") }, ACCEPTED)).toBe("skip");
    // An empty role list is "holds none of them", not "unknown".
    expect(readDecision({ state: "connected", me: asMe() }, ACCEPTED)).toBe("skip");
  });

  it.each<SessionState>(["disconnected", "degraded", "session-expired", "forbidden", "demo"])(
    "asks in the %s state when there is no identity to consult: the server stays the authority",
    (state) => {
      expect(readDecision({ state, me: null }, ACCEPTED)).toBe("ask");
    },
  );

  it("still skips a session that is known not to be admitted, in any settled state", () => {
    expect(readDecision({ state: "degraded", me: asMe("Analyst") }, ACCEPTED)).toBe("skip");
  });
});

describe("roleAllows and roleHolds: what to show", () => {
  it("roleAllows fails open on unknown roles; roleHolds fails closed", () => {
    expect(roleAllows(undefined, ACCEPTED)).toBe(true);
    expect(roleHolds(undefined, ACCEPTED)).toBe(false);
  });

  it("agree once roles are known", () => {
    expect(roleAllows(["Reviewer"], ACCEPTED)).toBe(true);
    expect(roleHolds(["Reviewer"], ACCEPTED)).toBe(true);
    expect(roleAllows(["Viewer"], ACCEPTED)).toBe(false);
    expect(roleHolds([], ACCEPTED)).toBe(false);
  });
});
