import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import "./VirtualList.css";

/* ---------------------------------------------------------------------------
   UX-15: the Catalog pattern's virtualization half, generalized.

   `CatalogTable` (UX-11) proved the shape for a fixed-row-height grid at
   1,000,000 rows. Every screen UX-15 migrates is bounded far below that (the
   review queue caps at 1,000, Studio change sets at 200, a marketplace page
   at 200, a refusals page at 200) — but "bounded" is not the same claim as
   "small enough to skip virtualization," and the tracker's exit condition
   asks for the pattern "in full," not only where the row count forces it.
   This is that same windowed-DOM idiom (only the visible slice mounts,
   `aria-setsize`/absolute `aria-posinset` so a screen reader announces the
   true position, `onReachEnd` for keyset/offset paging on approach to the
   loaded window's end) factored out once instead of re-implemented per
   screen with a slightly different bug each time.

   THE POSITION ATTRIBUTES ARE `setsize`/`posinset`, NOT `rowcount`/`rowindex`.
   This carried `aria-rowcount` on the container and `aria-rowindex` on each
   row until an axe-core run against the running app flagged it. Those two
   attributes are defined only on `grid`, `table` and `treegrid` and their
   rows; on a `list`/`listitem` they are invalid and, worse, simply ignored --
   so the announcement this component exists to provide was never being made.
   `aria-setsize`/`aria-posinset` are the list-shaped equivalents and say the
   same thing: item N of a total larger than the DOM holds. `CatalogTable` is
   a real `role="grid"`, so it correctly keeps `rowcount`/`rowindex`.

   Unlike `CatalogTable`'s fixed 38px row, list items here (proposal cards,
   marketplace cards, change-set rows) vary in height, so this measures each
   rendered element (`virtualizer.measureElement`) rather than assuming a
   constant — the same `@tanstack/react-virtual` API, a different sizing
   strategy for content that cannot be truncated to one line.
--------------------------------------------------------------------------- */

export interface VirtualListProps<T> {
  items: readonly T[];
  getKey: (item: T, index: number) => string;
  renderItem: (item: T, index: number) => React.ReactNode;
  /** Rough initial size in px before an item is first measured. */
  estimateSize?: number;
  /** True total, when known (`null` mid-keyset-page, matching CatalogTable). */
  totalCount?: number | null;
  onReachEnd?: () => void;
  loadingMore?: boolean;
  ariaLabel: string;
  emptyState?: React.ReactNode;
}

export function VirtualList<T>({
  items,
  getKey,
  renderItem,
  estimateSize = 90,
  totalCount,
  onReachEnd,
  loadingMore,
  ariaLabel,
  emptyState,
}: VirtualListProps<T>) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan: 6,
    // jsdom (and any host with a no-op ResizeObserver, see test/setup.ts)
    // never reports a real viewport size, so the virtualizer's own default
    // `{width:0,height:0}` renders zero rows even when `items` is non-empty
    // -- a real bug this screen's own tests caught (CatalogTable's identical
    // call has never been exercised under a render test, so it carries the
    // same latent gap). A generous initial estimate keeps every screen using
    // this component rendering its rows in a real browser (recalculated on
    // first paint) and under jsdom alike (stays at this estimate).
    initialRect: { width: 1024, height: 640 },
  });

  /* Keyboard traversal (review 2026-09-05, F21 / UX section 7).

     Every row in these lists is already a real button, so a row could be
     ACTIVATED from the keyboard -- but reaching row 90 meant ninety Tab
     presses, and in a virtualized list the row you want may not be mounted at
     all. Arrow keys move between rows, Home/End jump to the ends, and the
     virtualizer is asked to bring an unmounted target into view first. Doing
     it here means every list that uses this component gets it, rather than
     each screen inventing part of it. */
  const focusRow = (index: number) => {
    const clamped = Math.max(0, Math.min(items.length - 1, index));
    const focusIn = () => {
      const row = parentRef.current?.querySelector<HTMLElement>(`[data-index="${clamped}"]`);
      const target = row?.querySelector<HTMLElement>(
        'a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])',
      );
      target?.focus();
    };
    virtualizer.scrollToIndex(clamped);
    // The row may not be mounted yet; the virtualizer renders it on the next
    // frame, so focus after it exists rather than silently doing nothing.
    focusIn();
    requestAnimationFrame(focusIn);
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const KEYS = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!KEYS.includes(event.key)) return;
    const row = (event.target as HTMLElement).closest<HTMLElement>("[data-index]");
    const current = row ? Number(row.dataset["index"]) : 0;
    if (!Number.isFinite(current)) return;
    event.preventDefault();
    if (event.key === "ArrowDown") focusRow(current + 1);
    else if (event.key === "ArrowUp") focusRow(current - 1);
    else if (event.key === "Home") focusRow(0);
    else focusRow(items.length - 1);
  };

  const virtualItems = virtualizer.getVirtualItems();
  const last = virtualItems[virtualItems.length - 1];
  if (onReachEnd && last && last.index >= items.length - 5 && !loadingMore) {
    queueMicrotask(onReachEnd);
  }

  if (items.length === 0 && emptyState) return <>{emptyState}</>;

  return (
    <div
      ref={parentRef}
      className="vlist"
      role="list"
      aria-label={ariaLabel}
      /* This element scrolls, so it must be reachable by keyboard. Where its
         rows are focusable the region is reachable through them, but lists
         whose rows are plain content (Operations' analysis runs) left a
         keyboard user with no way to scroll it at all -- axe-core
         `scrollable-region-focusable`. Making the container itself a tab stop
         fixes both cases and costs one stop. */
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualItems.map((v) => {
          const item = items[v.index];
          if (item === undefined) return null;
          return (
            <div
              key={getKey(item, v.index)}
              ref={virtualizer.measureElement}
              data-index={v.index}
              role="listitem"
              aria-setsize={totalCount ?? items.length}
              aria-posinset={v.index + 1}
              className="vlist__row"
              style={{ transform: `translateY(${v.start}px)` }}
            >
              {renderItem(item, v.index)}
            </div>
          );
        })}
      </div>
      {loadingMore ? (
        <div className="vlist__more" role="status">
          Loading more…
        </div>
      ) : null}
    </div>
  );
}
