/* ---------------------------------------------------------------------------
   The Stewardship workspace, in a real browser behind the production proxy
   (tracker R11-S13).

   `StewardshipWorkspace` folded three screens -- the Stewardship work queue,
   the bulk actions and Playbooks -- into one page with three views on `?view=`.
   Its vitest suite proves the components in jsdom; what jsdom cannot show is
   the part a reader of the address bar and a keyboard user meet:

     - an OLD saved link (`#/playbooks`, or a bulk link written before the
       workspace existed) still lands where it used to, through the real
       router and the real SPA fallback;
     - the tab bar is a real ARIA tablist a keyboard can drive, with focus
       following selection;
     - leaving a view with unrun edits asks first, and a declined prompt
       leaves the view, the focus and what was typed exactly where they were.

   The last one is a NATIVE confirm dialog, which jsdom does not model at all.
   Each case here was first walked through by hand in the same browser
   (2026-09-19) and is pinned so a regression is a red build, not a report.
--------------------------------------------------------------------------- */

import { expect, expectScreen, test } from "../support/journey";

test.describe("Stewardship workspace -- DataSteward", () => {
  test.use({ identity: "steward" });

  const tab = (page: import("@playwright/test").Page, name: string) =>
    page.getByRole("tab", { name, exact: true });

  test("the retired #/playbooks link lands on the Automation view", async ({ page }) => {
    await page.goto("/#/playbooks");

    await expectScreen(page, "stewardship");
    await expect(tab(page, "Automation")).toHaveAttribute("aria-selected", "true");
    // The view is in the query string, so a reload or a shared link keeps it.
    await expect(page).toHaveURL(/[?&]view=automation/);

    await page.reload();
    await expectScreen(page, "stewardship");
    await expect(tab(page, "Automation")).toHaveAttribute("aria-selected", "true");
  });

  test("a bulk link written before the workspace existed opens Bulk actions with its filter", async ({
    page,
  }) => {
    // No `view=`: only the fields the old page carried. `stewardshipViewFrom` reads their
    // presence as "this was a bulk link".
    await page.goto("/?action=classify&field=TABLE_NAME&pattern=raw_%25#/steward/stewardship");

    await expectScreen(page, "stewardship");
    await expect(tab(page, "Bulk actions")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByPlaceholder("raw_%")).toHaveValue("raw_%");
    await expect(page.getByRole("combobox", { name: "Action" })).toHaveValue("classify");
  });

  test("the tabs are a keyboard tablist: arrows move focus and selection, and wrap", async ({
    page,
  }) => {
    await page.goto("/#/steward/stewardship");
    await expectScreen(page, "stewardship");

    await tab(page, "Work queue").focus();
    await expect(tab(page, "Work queue")).toHaveAttribute("aria-selected", "true");
    // One tab stop: only the selected tab is in the tab order.
    await expect(tab(page, "Bulk actions")).toHaveAttribute("tabindex", "-1");
    await expect(tab(page, "Automation")).toHaveAttribute("tabindex", "-1");

    await page.keyboard.press("ArrowRight");
    await expect(tab(page, "Bulk actions")).toHaveAttribute("aria-selected", "true");
    await expect(tab(page, "Bulk actions")).toBeFocused();

    await page.keyboard.press("ArrowRight");
    await expect(tab(page, "Automation")).toBeFocused();

    // Wraps at the end ...
    await page.keyboard.press("ArrowRight");
    await expect(tab(page, "Work queue")).toBeFocused();
    await expect(tab(page, "Work queue")).toHaveAttribute("aria-selected", "true");

    // ... and Home / End go to the ends.
    await page.keyboard.press("End");
    await expect(tab(page, "Automation")).toBeFocused();
    await page.keyboard.press("Home");
    await expect(tab(page, "Work queue")).toBeFocused();
  });

  test("leaving Bulk actions with an unrun edit asks first; declining keeps everything", async ({
    page,
  }) => {
    await page.goto("/#/steward/stewardship");
    await expectScreen(page, "stewardship");
    await tab(page, "Bulk actions").click();
    await expect(tab(page, "Bulk actions")).toHaveAttribute("aria-selected", "true");

    // The tag KEY is a write parameter (the match pattern is shareable filter state and does
    // not count -- it lives in the URL).
    const tagKey = page.getByPlaceholder("pii-reviewed");
    await tagKey.fill("pii-checked");

    // Decline: a native confirm that says what would be lost.
    const dialogs: string[] = [];
    page.once("dialog", async (dialog) => {
      dialogs.push(dialog.type());
      await dialog.dismiss();
    });
    await tab(page, "Work queue").click();

    await expect.poll(() => dialogs).toEqual(["confirm"]);
    await expect(tab(page, "Bulk actions")).toHaveAttribute("aria-selected", "true");
    await expect(tagKey).toHaveValue("pii-checked");

    // Accept: now it leaves.
    page.once("dialog", async (dialog) => {
      await dialog.accept();
    });
    await tab(page, "Work queue").click();
    await expect(tab(page, "Work queue")).toHaveAttribute("aria-selected", "true");
  });

  test("leaving a view with nothing unrun does not ask", async ({ page }) => {
    await page.goto("/#/steward/stewardship");
    await expectScreen(page, "stewardship");
    await tab(page, "Bulk actions").click();

    let asked = false;
    page.on("dialog", async (dialog) => {
      asked = true;
      await dialog.dismiss();
    });
    await tab(page, "Automation").click();

    await expect(tab(page, "Automation")).toHaveAttribute("aria-selected", "true");
    expect(asked).toBe(false);
  });
});
