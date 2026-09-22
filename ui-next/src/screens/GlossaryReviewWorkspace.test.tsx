import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { resetLocationCacheForTests } from "../lib/location";
import { useUrlState } from "../lib/useUrlState";
import { expectNoAxeViolations } from "../test/a11y";
import { GlossaryReviewWorkspace, glossaryReviewViewFrom } from "./GlossaryReviewWorkspace";

/* ---------------------------------------------------------------------------
   Glossary review -- the workspace SHELL (R11-AUD08).

   The two tabs are components with their own tests (`GlossaryConflicts.test`,
   `GlossaryLinkProposals.test`). What is only true of the shell is here:

     * the view axis is the URL, so a tab is a link and survives a reload, and
       the default is written as the ABSENCE of `?view=`;
     * a link that names a view the shell does not know still opens one;
     * the status filter belongs to a tab -- the two do not share a vocabulary --
       so it does not survive a switch;
     * the keyboard model of a tablist: one tab stop, arrows with wrap, Home/End.

   The tabs are stubbed, because a test about the shell that mounts two real
   screens is a test about two screens' mocks.
--------------------------------------------------------------------------- */

vi.mock("./GlossaryConflicts", () => ({
  GlossaryConflicts: () => {
    const [params] = useUrlState();
    return <p>conflicts content, status={params.get("status") ?? "none"}</p>;
  },
}));
vi.mock("./GlossaryLinkProposals", () => ({
  GlossaryLinkProposals: () => <p>proposals content</p>,
}));

function mount(url = "/#/steward/glossary-review") {
  history.replaceState(null, "", url);
  resetLocationCacheForTests();
  return render(<GlossaryReviewWorkspace />);
}

const viewParam = () => new URLSearchParams(location.search).get("view");
const statusParam = () => new URLSearchParams(location.search).get("status");
const tab = (name: string) => screen.getByRole("tab", { name });

beforeEach(() => {
  history.replaceState(null, "", "/");
  resetLocationCacheForTests();
});

describe("the Glossary review view axis", () => {
  it("leads with Conflicts, names the page, and keeps the canonical URL free of ?view=", async () => {
    mount();

    expect(await screen.findByText(/conflicts content/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Glossary review" })).toBeInTheDocument();
    expect(tab("Conflicts")).toHaveAttribute("aria-selected", "true");
    expect(viewParam()).toBeNull();
    expect(screen.getAllByRole("tab").map((element) => element.textContent)).toEqual(["Conflicts", "Link proposals"]);
    // The panel is named by the tab in front of it.
    expect(screen.getByRole("tabpanel", { name: "Conflicts" })).toBeInTheDocument();
  });

  it("puts the view in the URL, so a tab is a link", async () => {
    mount();
    await screen.findByText(/conflicts content/);

    fireEvent.click(tab("Link proposals"));
    await waitFor(() => expect(viewParam()).toBe("proposals"));
    expect(await screen.findByText("proposals content")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: "Link proposals" })).toBeInTheDocument();

    // Back to the default drops the field rather than writing `?view=conflicts`,
    // so one view does not have two spellings.
    fireEvent.click(tab("Conflicts"));
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(await screen.findByText(/conflicts content/)).toBeInTheDocument();
  });

  it("opens the view a URL names", async () => {
    mount("/?view=proposals#/steward/glossary-review");

    expect(await screen.findByText("proposals content")).toBeInTheDocument();
    expect(tab("Link proposals")).toHaveAttribute("aria-selected", "true");
    expect(tab("Conflicts")).toHaveAttribute("aria-selected", "false");
  });

  it("falls back to Conflicts for a ?view= value it does not know", async () => {
    mount("/?view=whatever#/steward/glossary-review");

    // A link from the future, or a typo. Either way the workspace opens.
    expect(await screen.findByText(/conflicts content/)).toBeInTheDocument();
    expect(glossaryReviewViewFrom(new URLSearchParams("view=whatever"))).toBe("conflicts");
    expect(glossaryReviewViewFrom(new URLSearchParams("view=proposals"))).toBe("proposals");
    expect(glossaryReviewViewFrom(new URLSearchParams(""))).toBe("conflicts");
  });

  it("drops the status filter on a switch, because the two tabs do not share a status vocabulary", async () => {
    mount("/?status=OPEN#/steward/glossary-review");
    expect(await screen.findByText("conflicts content, status=OPEN")).toBeInTheDocument();

    fireEvent.click(tab("Link proposals"));

    await waitFor(() => expect(viewParam()).toBe("proposals"));
    expect(statusParam()).toBeNull();
    await screen.findByText("proposals content");

    // ... and back, so a proposals filter is not carried into Conflicts either.
    history.replaceState(null, "", "/?view=proposals&status=DRAFT#/steward/glossary-review");
    resetLocationCacheForTests();
    fireEvent.click(tab("Conflicts"));
    await waitFor(() => expect(statusParam()).toBeNull());
  });

  it("does not rewrite the URL when the tab already in front is clicked", async () => {
    mount("/?status=OPEN#/steward/glossary-review");
    await screen.findByText("conflicts content, status=OPEN");

    fireEvent.click(tab("Conflicts"));

    // Nothing changed, so the filter the steward set is still there.
    expect(statusParam()).toBe("OPEN");
  });
});

describe("the tablist's keyboard model", () => {
  it("has one tab stop: the selected tab is tabbable and the other is not", async () => {
    mount();
    await screen.findByText(/conflicts content/);

    expect(tab("Conflicts")).toHaveAttribute("tabindex", "0");
    expect(tab("Link proposals")).toHaveAttribute("tabindex", "-1");
    // Only the selected tab names the panel; the other view is not mounted.
    expect(tab("Conflicts")).toHaveAttribute("aria-controls", "glrev-panel");
    expect(tab("Link proposals")).not.toHaveAttribute("aria-controls");
  });

  it("moves between tabs with the arrows, wrapping at both ends, and focus follows", async () => {
    mount();
    await screen.findByText(/conflicts content/);

    tab("Conflicts").focus();
    fireEvent.keyDown(tab("Conflicts"), { key: "ArrowRight" });
    await waitFor(() => expect(viewParam()).toBe("proposals"));
    expect(document.activeElement).toBe(tab("Link proposals"));

    fireEvent.keyDown(tab("Link proposals"), { key: "ArrowRight" }); // wraps to the first
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(document.activeElement).toBe(tab("Conflicts"));

    fireEvent.keyDown(tab("Conflicts"), { key: "ArrowLeft" }); // wraps to the last
    await waitFor(() => expect(viewParam()).toBe("proposals"));
    expect(document.activeElement).toBe(tab("Link proposals"));
  });

  it("jumps to the ends with Home and End, and ignores other keys", async () => {
    mount("/?view=proposals#/steward/glossary-review");
    await screen.findByText("proposals content");

    fireEvent.keyDown(tab("Link proposals"), { key: "Home" });
    await waitFor(() => expect(viewParam()).toBeNull());
    expect(document.activeElement).toBe(tab("Conflicts"));

    fireEvent.keyDown(tab("Conflicts"), { key: "End" });
    await waitFor(() => expect(viewParam()).toBe("proposals"));

    fireEvent.keyDown(tab("Link proposals"), { key: "a" });
    expect(viewParam()).toBe("proposals");
  });
});

describe("the shell's accessibility", () => {
  it("has no WCAG A/AA violations on either tab", async () => {
    const view = mount();
    await screen.findByText(/conflicts content/);
    await expectNoAxeViolations(view.container);

    fireEvent.click(tab("Link proposals"));
    await screen.findByText("proposals content");
    await expectNoAxeViolations(view.container);
  });
});
