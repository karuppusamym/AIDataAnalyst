import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

/* ---------------------------------------------------------------------------
   A governed refusal is not a broken connection.

   Found by walking the Ask journey in a browser against a live deployment:
   asking a question the approved tool must refuse (it needs `branch_code`)
   flipped the shell's status badge to "DEGRADED -- Requests are failing",
   while the refusal itself rendered correctly right underneath it. The
   platform was working exactly as designed and the chrome said it was broken.

   The cause was an exemption list of `{404, 422}` guarding a rule whose own
   comment had it right -- "answers about a resource or a request, not about
   the connection" -- and a governed refusal is a **409**: a tool that needs a
   parameter, an ambiguous governed term, a disabled datasource.

   These pin the distinction in both directions, because widening the list too
   far would hide a genuinely broken backend.
--------------------------------------------------------------------------- */

type Outcome = { ok: boolean; at: number; status: number; error?: Error };

let emit: ((outcome: Outcome) => void) | null = null;

// Only `observeRequests` is replaced; `ApiError` and the rest of the transport
// stay real, because the state machine branches on `ApiError`'s own predicates
// and a hand-rolled stand-in would let this test pass against a shape the app
// never produces.
vi.mock("./http", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./http")>()),
  observeRequests: (listener: (outcome: Outcome) => void) => {
    emit = listener;
    return () => {
      emit = null;
    };
  },
}));

// `USE_FIXTURES` short-circuits the whole state machine to "demo", so a
// fixture build would make every assertion below vacuous.
vi.mock("./appConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./appConfig")>()),
  USE_FIXTURES: false,
}));

import { SessionProvider, useSession } from "./session";

function Probe() {
  const session = useSession();
  return <span data-testid="state">{session.state}</span>;
}

function renderShell() {
  return render(
    <SessionProvider fetchMe={async () => ({ principal_id: "p", roles: ["Analyst"] }) as never}>
      <Probe />
    </SessionProvider>,
  );
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

async function send(outcome: Outcome) {
  await act(async () => {
    emit?.(outcome);
    await Promise.resolve();
  });
}

describe("the shell's connection state", () => {
  it("stays connected when the platform refuses a governed request", async () => {
    renderShell();
    await settle();
    await send({ ok: true, at: Date.now(), status: 200 });
    expect(screen.getByTestId("state")).toHaveTextContent("connected");

    // 409 MISSING_TOOL_PARAMETERS -- the refusal the Ask journey is built on.
    await send({
      ok: false,
      at: Date.now(),
      status: 409,
      error: new Error("approved tool requires parameters: branch_code"),
    });

    expect(screen.getByTestId("state")).toHaveTextContent("connected");
  });

  it("stays connected for the other statuses that are answers about a request", async () => {
    renderShell();
    await settle();
    await send({ ok: true, at: Date.now(), status: 200 });

    for (const status of [400, 404, 409, 415, 422]) {
      await send({ ok: false, at: Date.now(), status, error: new Error(`status ${status}`) });
      expect(screen.getByTestId("state")).toHaveTextContent("connected");
    }
  });

  it("still degrades when the backend is genuinely failing", async () => {
    renderShell();
    await settle();
    await send({ ok: true, at: Date.now(), status: 200 });

    await send({ ok: false, at: Date.now(), status: 500, error: new Error("boom") });

    expect(screen.getByTestId("state")).toHaveTextContent("degraded");
  });

  it("still degrades on a timeout or a throttle, which are about the link", async () => {
    renderShell();
    await settle();
    await send({ ok: true, at: Date.now(), status: 200 });

    await send({ ok: false, at: Date.now(), status: 429, error: new Error("slow down") });

    expect(screen.getByTestId("state")).toHaveTextContent("degraded");
  });
});
