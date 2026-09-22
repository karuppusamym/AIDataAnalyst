import { useCallback, useRef } from "react";
import type { KeyboardEvent } from "react";
import { CrossLinks } from "../components/CrossLinks";
import type { CrossLink } from "../components/CrossLinks";
import { useUrlState } from "../lib/useUrlState";
import { useUnsavedNavigationGuard } from "../lib/unsavedChanges";
import { OwnershipAssignments } from "./OwnershipAssignments";
import { OwnershipLeaver } from "./OwnershipLeaver";
import { OwnershipRules } from "./OwnershipRules";
import "./OwnershipScreen.css";

/* ---------------------------------------------------------------------------
   Ownership -- who owns what, and the governed ways to change it
   (R11-AUD08, part 2).

   THREE ROUTES HAD NO SCREEN, and a fourth had only a banner. Ownership rules,
   applying one, and reassigning a leaver's ownerships were reachable through the
   API and nowhere else; the assignments listing and the reaffirm routes had one
   consumer, the "your ownerships are expiring" banner on the Stewardship work
   queue. A steward who wanted to say "every `retail` table belongs to the retail
   stewards", or "Priya has left, hand her tables to Morgan", was told to use
   curl.

   ONE SCREEN, THREE VIEWS, because they are one question asked three ways -- what
   is owned, how do owners get assigned in bulk, and what happens when an owner
   goes. It lives in the Steward work area: a steward is who does all three. The
   Work queue (unowned assets, your own expiring ownerships) stays where it is and
   links here; this screen links back to it and to the Review queue.

   WHAT EVERY VIEW HAS TO KEEP SAYING, because the verbs suggest otherwise: NOTHING
   HERE CHANGES AN OWNER BY ITSELF. A rule applied and a leaver reassigned each open a
   review, and a different principal has to approve it (maker-checker). Reaffirming
   is the one direct write, and it extends an expiry -- it never changes who owns
   what. `lib/api/ownership.ts` has the handler-level account; the panels put the
   consequence beside each button.

   SHELL, NOT REWRITE. The tab pattern is the Stewardship workspace's, the roles
   are each panel's own (copied from the surface-control matrix beside the request
   they guard), and a tab switch that would discard a typed rule or an unsent
   reassignment asks first -- the same `lib/unsavedChanges` registry the bulk form
   reports into, because a tab switch is a `patchQuery` and bypasses the shell's
   own navigation guard.
--------------------------------------------------------------------------- */

export const OWNERSHIP_VIEWS = [
  {
    value: "assignments",
    label: "Assignments",
    scope:
      "This organization — every active ownership assignment, newest first. Reaffirm the ones you own to extend them; a PlatformAdmin or MetadataAdmin can reaffirm any.",
  },
  {
    value: "rules",
    label: "Rules",
    scope:
      "This organization — standing rules that match tables by name, schema, domain or tag. Applying one asks a reviewer; nothing is assigned until they approve it.",
  },
  {
    value: "leaver",
    label: "Leaver reassignment",
    scope:
      "This organization — one request moves what a leaver owns to a successor. It asks a reviewer; nothing moves until they approve it.",
  },
] as const;

export type OwnershipView = (typeof OWNERSHIP_VIEWS)[number]["value"];

const DEFAULT_VIEW: OwnershipView = "assignments";

/** Parse `?view=`. An unknown value opens the default rather than nothing: a link from the future is still a link to ownership. */
export function ownershipViewFrom(params: URLSearchParams): OwnershipView {
  const raw = params.get("view");
  return OWNERSHIP_VIEWS.find((entry) => entry.value === raw)?.value ?? DEFAULT_VIEW;
}

/** Where the adjacent jobs live. Each target keeps its own scope and authorization; a link is a request. */
const RELATED: CrossLink[] = [
  {
    screen: "stewardship",
    label: "Unowned assets",
    title: "Tables with no owner, and any ownership of yours about to lapse, in the Stewardship work queue.",
  },
  {
    screen: "governance",
    label: "Review queue",
    title: "Where a different reviewer approves or rejects the requests made here.",
  },
];

const PANEL_ID = "own-panel";
const tabId = (view: OwnershipView) => `own-tab-${view}`;

export function OwnershipScreen() {
  const [params, setParams] = useUrlState();
  const view = ownershipViewFrom(params);
  const confirmSwitch = useUnsavedNavigationGuard();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const active = OWNERSHIP_VIEWS.find((entry) => entry.value === view)!;

  /** Switch views. False when the unsaved-change prompt was declined. */
  const show = useCallback(
    (next: OwnershipView): boolean => {
      if (next === view) return true;
      // Asked here because a tab switch is a `patchQuery`, which by design bypasses the shell's
      // navigation guard. Declining leaves the view where it was.
      if (!confirmSwitch()) return false;
      // Only `view` is written: the assignments filter stays in the URL for when the steward returns.
      setParams({ view: next === DEFAULT_VIEW ? null : next });
      return true;
    },
    [view, confirmSwitch, setParams],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = OWNERSHIP_VIEWS.length - 1;
    const target =
      event.key === "ArrowRight"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowLeft"
          ? index === 0
            ? last
            : index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (target === null) return;
    event.preventDefault();
    // Focus follows the selection, so a declined switch keeps both where they were.
    if (show(OWNERSHIP_VIEWS[target]!.value)) tabs.current[target]?.focus();
  };

  return (
    <div className="ownws">
      <header className="ownws__head">
        <h1 className="ownws__h1">Ownership</h1>
        <p className="ownws__lede">
          Who owns what, the rules that assign owners in bulk, and the governed way to hand a leaver&rsquo;s ownerships to
          someone else. Anything that changes an owner is a request a different reviewer decides.
        </p>
      </header>

      <div className="ownws__tabs" role="tablist" aria-label="Ownership views">
        {OWNERSHIP_VIEWS.map((entry, index) => {
          const selected = view === entry.value;
          return (
            <button
              key={entry.value}
              ref={(element) => {
                tabs.current[index] = element;
              }}
              type="button"
              role="tab"
              id={tabId(entry.value)}
              aria-selected={selected}
              /* Only the selected tab names the panel: the other views are not mounted, and an
                 `aria-controls` pointing at nothing is an invalid reference rather than a hint. */
              aria-controls={selected ? PANEL_ID : undefined}
              tabIndex={selected ? 0 : -1}
              className={`ownws__tab${selected ? " ownws__tab--active" : ""}`}
              onClick={() => show(entry.value)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {entry.label}
            </button>
          );
        })}
      </div>
      <p className="ownws__scope">{active.scope}</p>
      <div className="ownws__links">
        <CrossLinks label="Related work" links={RELATED} />
      </div>

      <div className="ownws__panel" id={PANEL_ID} role="tabpanel" aria-labelledby={tabId(view)}>
        {view === "rules" ? <OwnershipRules /> : view === "leaver" ? <OwnershipLeaver /> : <OwnershipAssignments />}
      </div>
    </div>
  );
}
