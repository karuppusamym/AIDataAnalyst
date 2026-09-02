import { useCallback, useState } from "react";

/** Filters/selection live in the URL so a filtered/selected view is shareable
 *  (UX-7's own rationale for the evidence pane's permalink, generalized) --
 *  the same hook every migrated screen since `CatalogScreen` has copied
 *  verbatim (`CatalogScreen`/`NarratedLineageScreen`/`ReviewQueueScreen`/
 *  `MarketplaceScreen`/`LineageRefusalScreen`/`StudioChangeSetsScreen`).
 *  Extracted here so screens migrated from this point on share one
 *  implementation instead of adding a seventh copy; the six screens above are
 *  left with their own inline copy rather than touched in passing. */
export function useUrlState() {
  const [params, setParams] = useState(() => new URLSearchParams(location.search));
  const update = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      history.replaceState(null, "", `${location.pathname}?${next}`);
      return next;
    });
  }, []);
  return [params, update] as const;
}
