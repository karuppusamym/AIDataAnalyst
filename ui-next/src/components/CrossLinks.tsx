import { navigateTo } from "../lib/navigate";
import "./CrossLinks.css";

/* ---------------------------------------------------------------------------
   CrossLinks — the joins between screens.

   Atlas's value is not in any one screen; it is in the fact that a table has
   lineage, and quality, and meaning, and an owner, and a policy, all resolved
   against the same identifier. The shell had thirty-odd screens and four
   cross-screen links in total, so a person who found a table in the Catalog
   and wanted its lineage had to open Lineage, re-pick the datasource, and
   search for the table again — losing the selection every time.

   Every screen already keeps its selection in the query string
   (`useUrlState`), and `navigateTo` writes exactly that shape, so a link from
   here lands on the target screen with the right row already focused. This
   component exists so that behaviour is written once and looks the same
   everywhere rather than being re-invented per screen.

   Deliberately not a router: it takes screen ids that already exist in
   `App.tsx`'s NAV, and a link to an id that is not in NAV simply resolves to
   Overview rather than throwing — the same behaviour a stale bookmark gets.
--------------------------------------------------------------------------- */

export interface CrossLink {
  /** A NAV id from `App.tsx` (`lineage`, `quality`, `context`, …). */
  screen: string;
  label: string;
  /** Query params the target screen reads through `useUrlState`. */
  params?: Record<string, string>;
  /** Rendered as the button's tooltip — say what the target will show. */
  title?: string;
}

export function CrossLinks({
  links,
  label = "Related",
}: {
  links: CrossLink[];
  label?: string;
}) {
  if (links.length === 0) return null;
  return (
    <nav className="xlinks" aria-label={label}>
      <span className="xlinks__label">{label}</span>
      <div className="xlinks__row">
        {links.map((link) => (
          <button
            key={`${link.screen}:${link.label}`}
            type="button"
            className="xlinks__link"
            title={link.title}
            onClick={() => navigateTo(link.screen, link.params ?? {})}
          >
            {link.label}
            <span aria-hidden="true"> →</span>
          </button>
        ))}
      </div>
    </nav>
  );
}
