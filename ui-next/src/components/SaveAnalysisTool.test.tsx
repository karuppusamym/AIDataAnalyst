import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SaveAnalysisTool } from "./SaveAnalysisTool";

/* ---------------------------------------------------------------------------
   R11-B1 — proposing an answer as a governed tool.

   This action existed and was untested, which for a governance surface is the
   wrong way round: what matters here is not that a draft gets created but
   that the screen cannot be used to put a tool into service. Three properties
   carry that weight, and each has a test whose failure would be a real
   regression rather than a copy change:

   1. the draft is created only after the author ticks the attestation, so a
      blueprint cannot be waved through unread;
   2. what is created is a DRAFT and the screen says it still needs
      independent review -- the Ask screen is not a way to mint tools;
   3. the parameters the author typed are what is submitted, because the
      stored SQL has redacted values and a wrong parameter type is how a
      governed tool ends up refusing every caller.

   The API layer is mocked at the module boundary: these are assertions about
   the screen's own rules, and the endpoints themselves are covered by
   `tests/test_seeded_governed_tool_journey.py` against the real app.
--------------------------------------------------------------------------- */

const BLUEPRINT = {
  project_id: "proj_core",
  parameter_review_required: true,
  definition: {
    slug: "monthly_net_revenue",
    name: "Monthly net revenue",
    description: "Net revenue by month.",
    datasource_id: "ds_snowflake_prod",
    semantic_model_version_id: null,
    sql_template: "SELECT 1",
    parameters: [],
    allowed_roles: ["Analyst"],
  },
};

const fetchAnalysisToolBlueprint = vi.fn();
const createToolVersion = vi.fn();

vi.mock("../lib/api", () => ({
  fetchAnalysisToolBlueprint: (...args: unknown[]) => fetchAnalysisToolBlueprint(...args),
  createToolVersion: (...args: unknown[]) => createToolVersion(...args),
}));

vi.mock("../lib/navigate", () => ({ navigateTo: vi.fn() }));

beforeEach(() => {
  fetchAnalysisToolBlueprint.mockReset();
  createToolVersion.mockReset();
  fetchAnalysisToolBlueprint.mockResolvedValue(structuredClone(BLUEPRINT));
  createToolVersion.mockResolvedValue({ id: "tv_monthly_net_revenue_1", status: "DRAFT" });
});

async function prepare() {
  render(<SaveAnalysisTool runId="ar_1" />);
  fireEvent.click(screen.getByText("Prepare draft tool"));
  await waitFor(() => expect(fetchAnalysisToolBlueprint).toHaveBeenCalledWith("ar_1"));
}

describe("proposing an answer as a governed tool", () => {
  it("asks the run for its own blueprint rather than taking one from the caller", async () => {
    await prepare();

    // The blueprint comes from what the run executed. A screen that let the
    // author supply the SQL would make "propose this answer as a tool" a
    // different and much weaker claim.
    expect(fetchAnalysisToolBlueprint).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByDisplayValue("monthly_net_revenue")).toBeTruthy(),
    );
  });

  it("will not create the draft until the author attests to the SQL", async () => {
    await prepare();

    const save = await waitFor(() => screen.getByText("Save as draft tool"));
    fireEvent.click(save);

    // Clicking a disabled button must do nothing at all -- not fail later.
    expect(createToolVersion).not.toHaveBeenCalled();
  });

  it("creates the draft once the attestation is ticked", async () => {
    await prepare();

    fireEvent.click(await waitFor(() => screen.getByRole("checkbox")));
    fireEvent.click(screen.getByText("Save as draft tool"));

    await waitFor(() => expect(createToolVersion).toHaveBeenCalledTimes(1));
    expect(createToolVersion.mock.calls[0]?.[0]).toBe("proj_core");
  });

  it("says the draft still needs independent review", async () => {
    await prepare();
    fireEvent.click(await waitFor(() => screen.getByRole("checkbox")));
    fireEvent.click(screen.getByText("Save as draft tool"));

    // The load-bearing sentence. Without it a steward can reasonably believe
    // the tool is now usable, and it is not: it is a DRAFT awaiting a second
    // identity.
    const status = await waitFor(() => screen.getByRole("status"));
    expect(status.textContent).toMatch(/independent review/i);
  });

  it("submits the parameter definitions the author typed", async () => {
    await prepare();

    const json = await waitFor(() => screen.getByDisplayValue("[]"));
    fireEvent.change(json, {
      target: {
        value: JSON.stringify([{ name: "branch_code", type: "STRING", required: true }]),
      },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Save as draft tool"));

    await waitFor(() => expect(createToolVersion).toHaveBeenCalled());
    expect(createToolVersion.mock.calls[0]?.[1].parameters).toEqual([
      { name: "branch_code", type: "STRING", required: true },
    ]);
  });

  it("refuses parameter definitions that are not an array", async () => {
    await prepare();

    fireEvent.change(await waitFor(() => screen.getByDisplayValue("[]")), {
      target: { value: '{"name":"branch_code"}' },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByText("Save as draft tool"));

    const status = await waitFor(() => screen.getByRole("status"));
    expect(status.textContent).toMatch(/must be an array/i);
    expect(createToolVersion).not.toHaveBeenCalled();
  });

  it("reports a refusal from the server instead of appearing to succeed", async () => {
    createToolVersion.mockRejectedValue(new Error("tool authoring role is required"));
    await prepare();

    fireEvent.click(await waitFor(() => screen.getByRole("checkbox")));
    fireEvent.click(screen.getByText("Save as draft tool"));

    const status = await waitFor(() => screen.getByRole("status"));
    expect(status.textContent).toMatch(/tool authoring role is required/);
  });

  it("reports a refusal of the blueprint itself", async () => {
    // The endpoint refuses anyone but the run's own author, and that refusal
    // arrives on the first button -- where there is nothing on screen yet to
    // suggest anything happened.
    fetchAnalysisToolBlueprint.mockRejectedValue(
      new Error("only the run's author may propose it as a tool"),
    );
    render(<SaveAnalysisTool runId="ar_1" />);
    fireEvent.click(screen.getByText("Prepare draft tool"));

    const status = await waitFor(() => screen.getByRole("status"));
    expect(status.textContent).toMatch(/only the run's author/);
    expect(screen.queryByText("Save as draft tool")).toBeNull();
  });
});
