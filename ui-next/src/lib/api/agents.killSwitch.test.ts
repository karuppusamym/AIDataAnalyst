import { beforeEach, describe, expect, it, vi } from "vitest";

/* ---------------------------------------------------------------------------
   The model kill switch's three calls (MG-2, R11-AUD08): the URL and body each
   sends, and what each does under demo data.

   Under demo data the WRITES refuse and the READ answers "nothing engaged". The
   asymmetry is the point, and it is the same one `engageAgentKillSwitch` draws:
   a switch that silently did nothing is the worst thing to mock, while an
   organization that never used the switch genuinely has no rows -- that is what
   the live route answers for it, so `[]` is not a made-up state.
--------------------------------------------------------------------------- */

const { get, postJson, mode } = vi.hoisted(() => ({
  get: vi.fn(),
  postJson: vi.fn(),
  mode: { fixtures: false },
}));

vi.mock("./transport", () => ({
  get: (...args: unknown[]) => get(...args),
  postJson: (...args: unknown[]) => postJson(...args),
  putJson: vi.fn(),
  // Runs the live arm unless a test turns demo data on, in which case only the READ has a demo arm.
  demoOr: (demo: (fixtures: unknown) => Promise<unknown>, live: () => Promise<unknown>) =>
    mode.fixtures ? demo({}) : live(),
}));
vi.mock("../appConfig", () => ({
  get USE_FIXTURES() {
    return mode.fixtures;
  },
}));

import { engageModelKillSwitch, fetchModelKillSwitchState, releaseModelKillSwitch } from "./agents";

const ORG = "9b90b35f-dcf5-49d3-8f0e-2f269987ae87";

beforeEach(() => {
  mode.fixtures = false;
  get.mockReset();
  postJson.mockReset();
});

describe("the model kill switch API", () => {
  it("reads every switch row for the organization", async () => {
    const rows = [{ id: "k1", scope: "ORGANIZATION", route_key: "*", engaged: true }];
    get.mockResolvedValue(rows);
    const controller = new AbortController();

    await expect(fetchModelKillSwitchState(ORG, controller.signal)).resolves.toBe(rows);

    expect(get).toHaveBeenCalledWith(`/v1/organizations/${ORG}/kill-switch`, controller.signal);
  });

  it("engages with the reason, and leaves route_key out so the whole organization is meant", async () => {
    const state = { id: "k1", scope: "ORGANIZATION", route_key: "*", engaged: true };
    postJson.mockResolvedValue(state);
    const controller = new AbortController();

    await expect(
      engageModelKillSwitch(ORG, { reason: "provider incident 4471" }, controller.signal),
    ).resolves.toBe(state);

    expect(postJson).toHaveBeenCalledWith(
      `/v1/organizations/${ORG}/kill-switch/engage`,
      { reason: "provider incident 4471" },
      controller.signal,
    );
    expect(postJson.mock.calls[0]![1]).not.toHaveProperty("route_key");
  });

  it("releases with the reason", async () => {
    postJson.mockResolvedValue({ id: "k1", engaged: false });

    await releaseModelKillSwitch(ORG, { reason: "provider recovered" });

    expect(postJson).toHaveBeenCalledWith(
      `/v1/organizations/${ORG}/kill-switch/release`,
      { reason: "provider recovered" },
      undefined,
    );
  });

  it("lets a server refusal through untouched", async () => {
    const refusal = Object.assign(new Error("kill switch is not currently engaged"), { status: 409 });
    postJson.mockRejectedValue(refusal);

    await expect(releaseModelKillSwitch(ORG, { reason: "nothing to release" })).rejects.toBe(refusal);
  });

  it("refuses to engage or release under demo data, and sends nothing", async () => {
    mode.fixtures = true;

    await expect(engageModelKillSwitch(ORG, { reason: "drill" })).rejects.toThrow(
      "The kill switch is unavailable in demo data mode",
    );
    await expect(releaseModelKillSwitch(ORG, { reason: "drill" })).rejects.toThrow(
      "The kill switch is unavailable in demo data mode",
    );
    expect(postJson).not.toHaveBeenCalled();
  });

  it("reads as 'nothing engaged' under demo data, without a request", async () => {
    mode.fixtures = true;

    await expect(fetchModelKillSwitchState(ORG)).resolves.toEqual([]);
    expect(get).not.toHaveBeenCalled();
  });
});
