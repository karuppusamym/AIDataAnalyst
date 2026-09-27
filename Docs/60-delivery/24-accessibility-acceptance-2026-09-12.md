# Interactive accessibility acceptance — the human half (R11-C2)

*Written 2026-09-12. A dated snapshot, measured against the tree at the time
(`CLAUDE.md` rule 2). Status lives in tracker section P; this document is the
procedure and the evidence sheet, not a queue.*

R11-C2 carries F21, UX-5 and TS-9. Its own text says what this document exists
for: *"keyboard/screen reader plus 100% zoom/multiple screens; automated axe
alone is insufficient."*

The row splits cleanly in two.

**The automated half is done and is described in §1.** It is committed, it runs
in CI on every push, and it fails the build when it regresses.

**The half below §2 cannot be done by an agent and has not been done.** It needs
a person, a real assistive technology and real screens. Nothing in §2 is
claimed, simulated, or inferred. Until somebody executes §2 and records the
result, R11-C2 is PARTIAL, and that is the correct status — not a shortfall to
be papered over.

---

## 1. What is already proved, automatically

Run from `ui-next`:

```
npm run typecheck
npm run test
npm run build
```

The accessibility-specific coverage added for this row:

| File | What it holds the app to |
|---|---|
| `ui-next/src/test/a11y.ts` | The harness: axe-core at WCAG 2.1 A/AA, a real Tab driver, an accessible-name floor. |
| `ui-next/src/App.a11y.test.tsx` | The shell: skip link, focus on route change, the drawer's focus contract, and 60 Tab presses that cannot escape the command palette. |
| `ui-next/src/a11y-sweep.test.tsx` | All 44 navigable screens: no detectable WCAG A/AA violation, no unnamed focusable control. |
| `ui-next/src/components/primitives.a11y.test.tsx` | `Dialog`, `ConfirmDialog`, `AsyncState`, `useToast`, `FormErrors` — trap, restore, announce. |

### 1.1 Rules deliberately not run, and why

`color-contrast` is disabled in the harness (`RULES_JSDOM_CANNOT_JUDGE` in
`ui-next/src/test/a11y.ts`). It needs real rendering and a composited background
colour; jsdom lays nothing out, so running it produces an error or an
"incomplete", never an answer. **Contrast is therefore entirely on §2.**

### 1.2 The three things a green run does not mean

1. **No CSS is loaded in jsdom.** Every element is "visible" to the test
   runner. The Tab order the tests assert is an *over-approximation* of the
   real one — safe for proving focus is contained, useless for proving
   anything is hidden.
2. **There is no screen reader.** `getByRole(name:)` proves the accessibility
   tree carries the right name. It does not prove NVDA says it, says it at the
   right moment, or that the reading order is intelligible.
3. **The sweep runs against the bundled demo estate.** Screens that need a
   datasource chosen are swept in their empty or prompt state, so the markup
   behind a *populated* table is covered only where that screen's own test
   file renders it.

---

## 2. The procedure a person must execute

Nothing here has been done. Each check has a pass condition written so it can
be answered yes or no without asking the author.

### 2.0 Setup

1. Build and serve the app the way a user gets it — the built bundle, not the
   dev server:
   ```
   cd ui-next
   npm run build
   npm run preview
   ```
2. Do the whole of §2 in **Chrome or Edge on Windows with NVDA**, then repeat
   §2.2 only in **Safari on macOS with VoiceOver**. Those are the two pairings
   a bank's own accessibility audit will use. If only one is available, record
   which, and the row stays PARTIAL for the other.
3. Sign in so the shell is past `AuthBlockedScreen`; a blocked shell is a
   different (and much smaller) surface.

### 2.1 Keyboard, at 100% zoom, on one screen

Do this on **each of these six screens**, chosen because each is the only
instance of a structural idiom in this app:

| Screen | Route | Why this one |
|---|---|---|
| Catalog | `#/catalog` | Virtualized table (`CatalogTable`), the only windowed grid. |
| Ask Atlas | `#/analyst` | Free-text entry plus a governed result table. |
| Review queue | `#/governance` | Maker–checker decisions behind a `ConfirmDialog` that requires a rationale. |
| Unified lineage | `#/unified-lineage` | SVG diagram whose nodes are `role="button"` on `<g>`. |
| Access policies | `#/access-policies` | Horizontally scrollable table (changed by this row). |
| Sources | `#/sources` | Multi-step forms and file upload. |

On each one, confirm:

- [ ] **K1.** From a fresh page load, the *first* Tab press reveals a visible
      "Skip to main content" button at the top left. Enter on it moves focus
      into the page content, and the next Tab press lands on a control in the
      page — not back at the top of the sidebar.
- [ ] **K2.** Tab reaches every control that can be clicked. Nothing is
      reachable only with a mouse.
- [ ] **K3.** The focus ring is visible on every stop, against the colour
      behind it, including inside the dark sidebar and on top of any
      light-on-light hover state.
- [ ] **K4.** Focus order matches reading order. Focus never jumps backwards
      up the page or into an element that is off-screen.
- [ ] **K5.** Tab never becomes stuck. From any control, continued Tab presses
      eventually reach the browser's address bar.
- [ ] **K6.** Every control activates with Enter, and every control that looks
      like a button also activates with Space. Space does not scroll the page
      instead of activating.
- [ ] **K7.** Opening any dialog moves focus into it; Tab and Shift+Tab stay
      inside it; Escape closes it and focus returns to the control that opened
      it.

### 2.2 Screen reader (NVDA, then VoiceOver)

With the screen reader running and **the monitor switched off or eyes closed**
— reading the screen defeats the test:

- [ ] **S1.** Navigate by landmark (NVDA: `D`). The shell announces a
      navigation landmark ("Main"), a main landmark, and a region named for
      the current screen.
- [ ] **S2.** Choose a different page from the sidebar. Confirm the new screen
      is **announced by name** without further input. This is the specific
      behaviour added by this row; if nothing is announced, it has regressed.
- [ ] **S3.** Navigate by heading (`H`) on each of the six screens in §2.1.
      Confirm there is exactly one level-1 heading and no level is skipped.
- [ ] **S4.** On Review queue, approve or reject an item. Confirm that (a) the
      rationale box is announced with its own label and its hint read
      separately as a description, (b) the outcome — success or failure — is
      announced without moving focus to find it.
- [ ] **S5.** On Ask Atlas, ask a question. Confirm the wait is announced
      ("Loading…" or equivalent) and the arrival of the result is announced.
      A silent 8-second wait is a failure even though the markup is valid.
- [ ] **S6.** On Unified lineage, Tab to a diagram node. Confirm it announces
      as a button *with the asset's qualified name*, not as a run-on of the
      card's text.
- [ ] **S7.** Force a failure (disconnect the network, then retry a load).
      Confirm the error is announced, and that it says what to do.
- [ ] **S8.** Confirm no control announces as just "button", "link", "edit
      text", or "clickable".

### 2.3 Zoom and reflow

On Catalog and on Review queue:

- [ ] **Z1.** At browser zoom **100%**, in a maximised window on a 1920×1080
      screen, no text is clipped and no control overlaps another.
- [ ] **Z2.** At **200%** zoom, all content and functionality remain available
      (WCAG 1.4.4, AA).
- [ ] **Z3.** At **400%** zoom in a 1280px-wide window — which is the 320 CSS
      pixel reflow condition (WCAG 1.4.10, AA) — content reflows to a single
      column and **nothing requires horizontal scrolling** except the data
      tables, which are allowed to scroll and must be reachable by keyboard
      (Tab into the scroll region, then arrow keys).
- [ ] **Z4.** At 400%, the sidebar is a drawer. **This is the check for the
      defect this row fixed**: with the drawer *closed*, press Tab repeatedly
      from the top of the page and confirm focus never lands on an invisible
      control. Before the fix, roughly fourteen off-screen sidebar controls
      were in the Tab order with no visible focus indicator anywhere on
      screen. Automated tests cannot see this, because jsdom loads no CSS.
- [ ] **Z5.** Open the drawer with the ☰ button. Confirm focus moves into it,
      Escape closes it, and focus returns to ☰.

### 2.4 Multiple screens

- [ ] **M1.** With two monitors at **different scale factors** (e.g. 100% and
      150%), drag the browser window from one to the other. Confirm the layout
      re-renders correctly rather than staying at the old scale, and that text
      does not blur or clip.
- [ ] **M2.** Repeat with the browser straddling both monitors.
- [ ] **M3.** On the lower-resolution screen, confirm the six screens in §2.1
      have no horizontal page scrollbar at 100% zoom.

### 2.5 Contrast

Because §1.1 excludes it from the automated run:

- [ ] **C1.** Run the **axe DevTools browser extension** against the running
      app on each of the six screens in §2.1, with `color-contrast` enabled.
      Record every violation. This is the rule the test harness cannot run,
      and running it here is what closes that gap.
- [ ] **C2.** Confirm the focus indicator itself meets 3:1 against the
      adjacent colour (WCAG 1.4.11), including on the dark sidebar.

---

## 3. Recording the result

Record outcomes against the check ids above (K1–K7, S1–S8, Z1–Z5, M1–M3,
C1–C2), with the AT and version, the browser and version, the monitor
configuration, and the date. Failures become new tracker rows in section P;
this document is not a queue and must not be edited into one.

R11-C2 can move past PARTIAL only when §2 has been executed and recorded. A
green `npm run test` is **§1 only** and does not by itself justify any change
of status.
