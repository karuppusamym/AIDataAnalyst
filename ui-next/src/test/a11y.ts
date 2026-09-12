import axe from "axe-core";
import type { ElementContext, Result, RunOptions, Spec } from "axe-core";

/* ---------------------------------------------------------------------------
   Accessibility test support (R11-C2, carrying F21 · UX-5 · TS-9).

   WHAT THIS IS AND IS NOT. The tracker row says "automated axe alone is
   insufficient". That is the operating assumption here, not a caveat bolted
   on afterwards: this module exists so that the machine-checkable half is
   actually checked on every run, and so that the half a machine cannot check
   is visible as a named gap rather than as silence.

   Three things axe-core cannot tell you, all of which this suite therefore
   asserts by other means:

   1. **Whether the keyboard can reach and operate the control.** axe reads
      the rendered tree; it never presses Tab. `tabThrough` below does, via
      `@testing-library/user-event`, which implements sequential focus
      navigation in userland -- jsdom has none of its own. This matters
      concretely here: the 2026-09-05 review found that the command palette
      declared `role="dialog" aria-modal="true"` and still let Tab walk out
      into the sidebar behind it, and the note in `primitives.tsx` records
      that jsdom could not show it. It can now.

   2. **Whether an announcement actually happens.** A live region that exists
      and a live region that receives text are the same DOM to axe.

   3. **Whether the name a control carries is the name a person needs.**
      `aria-label="button"` passes every automated rule there is.

   And one axe cannot run at all in this environment: `color-contrast`
   requires real rendering, which jsdom does not do. It is disabled below
   rather than left to report a meaningless pass, and it is consequently part
   of what the human acceptance pass in
   `Docs/60-delivery/24-accessibility-acceptance-2026-09-12.md` must cover.
--------------------------------------------------------------------------- */

/** The standard the row is measured against: WCAG 2.1 Level A and AA. */
export const WCAG_AA_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] as const;

/**
 * Rules switched off because jsdom cannot answer them honestly, each with the
 * reason and the human check that covers it instead. A rule is disabled here
 * only when running it would produce a *meaningless* result -- never because
 * it failed.
 */
export const RULES_JSDOM_CANNOT_JUDGE: Readonly<Record<string, string>> = {
  "color-contrast":
    "Needs real rendering and a composited background colour; jsdom lays nothing out, "
    + "so axe either errors or reports 'incomplete'. Covered by the human 100%-zoom / "
    + "multi-screen acceptance pass, which is where contrast is actually visible.",
};

let configured = false;

/** `{ "color-contrast": { enabled: false }, ... }`, from the one list above. */
function disabledRules(): NonNullable<RunOptions["rules"]> {
  const rules: NonNullable<RunOptions["rules"]> = {};
  for (const id of Object.keys(RULES_JSDOM_CANNOT_JUDGE)) rules[id] = { enabled: false };
  return rules;
}

/**
 * Configure axe once for the whole test process.
 *
 * Called from `src/test/setup.ts`, which is the vitest `setupFiles` entry, so
 * a direct `axe.run` anywhere in the suite honours the same policy as
 * `axeViolations` below.
 */
export function configureAxeForJsdom(): void {
  if (configured) return;
  configured = true;
  const spec: Spec = {
    rules: Object.keys(RULES_JSDOM_CANNOT_JUDGE).map((id) => ({ id, enabled: false })),
  };
  axe.configure(spec);
}

/** Every WCAG A/AA violation axe can see in `container`. */
export async function axeViolations(
  container: ElementContext,
  options: RunOptions = {},
): Promise<Result[]> {
  configureAxeForJsdom();
  const results = await axe.run(container, {
    runOnly: { type: "tag", values: [...WCAG_AA_TAGS] },
    // `resultTypes` keeps axe from building the (large) pass list we never read.
    resultTypes: ["violations"],
    ...options,
    // Last, and merged rather than replaced: a `runOnly` tag selection
    // re-enables every rule carrying the tag, `axe.configure` included, so the
    // rules jsdom cannot judge have to be switched off in the *run* options or
    // `color-contrast` runs anyway -- straight into
    // `HTMLCanvasElement.getContext`, which jsdom does not implement.
    rules: { ...disabledRules(), ...options.rules },
  });
  return results.violations;
}

/** One violation, rendered so the failure names the element and the fix. */
function describeViolation(violation: Result): string {
  const where = violation.nodes
    .map((node) => `      - ${node.target.join(" ")}\n        ${node.failureSummary ?? ""}`)
    .join("\n");
  return `  [${violation.id}] ${violation.help} (${violation.impact ?? "unknown"} impact)\n${where}\n      ${violation.helpUrl}`;
}

/**
 * Assert that `container` has no WCAG A/AA violation axe can detect.
 *
 * Deliberately a plain function rather than an `expect.extend` matcher: the
 * matcher form needs a module augmentation of `vitest`'s `Assertion`
 * interface to survive `tsc --noEmit`, and that augmentation is a global
 * side-effect of importing a test helper. A function that throws is the same
 * assertion with none of that.
 */
export async function expectNoAxeViolations(
  container: ElementContext,
  options: RunOptions = {},
): Promise<void> {
  const violations = await axeViolations(container, options);
  if (violations.length === 0) return;
  throw new Error(
    `axe-core found ${violations.length} WCAG A/AA violation(s):\n`
      + violations.map(describeViolation).join("\n"),
  );
}

/* --- The half axe cannot do ---------------------------------------------- */

/**
 * Press Tab `steps` times and report what held focus after each press.
 *
 * Returns the elements, not their names, so a caller can assert on order,
 * on identity (`toBe(button)`), or on accessible name as it needs.
 */
export async function tabThrough(
  user: { tab: (options?: { shift?: boolean }) => Promise<void> },
  steps: number,
  options: { shift?: boolean } = {},
): Promise<Element[]> {
  const seen: Element[] = [];
  for (let index = 0; index < steps; index += 1) {
    await user.tab(options.shift ? { shift: true } : undefined);
    if (document.activeElement) seen.push(document.activeElement);
  }
  return seen;
}

/**
 * Tab `steps` times from wherever focus is and assert every stop stayed
 * inside `root`.
 *
 * This is the assertion that "the dialog traps focus" actually means. The
 * count matters: the palette escaped on the 52nd press, not the 2nd, so a
 * two-press test would have called the broken build correct.
 */
export async function expectFocusStaysWithin(
  user: { tab: (options?: { shift?: boolean }) => Promise<void> },
  root: HTMLElement,
  steps: number,
): Promise<void> {
  const escaped: string[] = [];
  for (let index = 0; index < steps; index += 1) {
    await user.tab();
    const active = document.activeElement;
    if (active && active !== document.body && !root.contains(active)) {
      escaped.push(`press ${index + 1}: <${active.tagName.toLowerCase()}> ${describeElement(active)}`);
    }
  }
  if (escaped.length > 0) {
    throw new Error(
      `Focus left the trapped region on ${escaped.length} of ${steps} Tab presses:\n  ${escaped.join("\n  ")}`,
    );
  }
}

function describeElement(element: Element): string {
  const label = element.getAttribute("aria-label");
  if (label) return `aria-label="${label}"`;
  const text = element.textContent?.trim().slice(0, 40);
  return text ? `"${text}"` : "(no name)";
}

/**
 * Every element that can hold focus inside `root`, in DOM order, that has no
 * accessible name.
 *
 * Not an axe rule: axe's `button-name`/`link-name` rules cover buttons and
 * links, but a focusable `role="button"` on an SVG `<g>` -- which this app
 * uses for both lineage diagrams -- is checked by neither.
 */
export function unnamedFocusableElements(root: HTMLElement): Element[] {
  const focusable = root.querySelectorAll<HTMLElement>(
    "a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])",
  );
  return Array.from(focusable).filter((element) => {
    // `tabindex="-1"` is programmatically focusable but not reachable by Tab,
    // and an `aria-hidden` subtree is not in the accessibility tree at all.
    // Neither is something a user can land on and be told nothing about, so
    // neither needs a name -- the shell's backdrop is both.
    if (element.getAttribute("tabindex") === "-1") return false;
    if (element.closest("[aria-hidden='true']")) return false;
    return accessibleNameOf(element).length === 0;
  });
}

/**
 * A deliberately small accessible-name computation: the attribute forms this
 * app actually uses, plus text content. Not a substitute for a screen
 * reader -- it is the floor, and the human pass is the ceiling.
 */
export function accessibleNameOf(element: Element): string {
  const label = element.getAttribute("aria-label");
  if (label?.trim()) return label.trim();
  const labelledBy = element.getAttribute("aria-labelledby");
  if (labelledBy) {
    const named = labelledBy
      .split(/\s+/)
      .map((id) => element.ownerDocument.getElementById(id)?.textContent?.trim() ?? "")
      .filter(Boolean)
      .join(" ");
    if (named) return named;
  }
  if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement
    || element instanceof HTMLTextAreaElement) {
    const labels = Array.from(element.labels ?? []);
    const fromLabel = labels.map((node) => node.textContent?.trim() ?? "").filter(Boolean).join(" ");
    if (fromLabel) return fromLabel;
    const title = element.getAttribute("title");
    if (title?.trim()) return title.trim();
    return "";
  }
  const svgTitle = element.querySelector(":scope > title")?.textContent?.trim();
  if (svgTitle) return svgTitle;
  const text = visibleText(element);
  if (text) return text;
  const title = element.getAttribute("title");
  return title?.trim() ?? "";
}

/**
 * `textContent` minus anything `aria-hidden`.
 *
 * This app labels almost every control with an icon glyph in an
 * `aria-hidden="true"` span next to the words. Counting those would report a
 * name for a control that in fact announces only "⌕", which is the exact
 * class of defect this file exists to catch.
 */
function visibleText(element: Element): string {
  let text = "";
  for (const node of Array.from(element.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent ?? "";
    } else if (node instanceof Element && node.getAttribute("aria-hidden") !== "true") {
      text += visibleText(node);
    }
  }
  return text.trim();
}
