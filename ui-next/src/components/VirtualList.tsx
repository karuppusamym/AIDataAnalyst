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
   `aria-rowcount`/absolute `aria-rowindex` so a screen reader announces the
   true position, `onReachEnd` for keyset/offset paging on approach to the
   loaded window's end) factored out once instead of re-implemented per
   screen with a slightly different bug each time.

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
      aria-rowcount={totalCount ?? items.length}
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
              aria-rowindex={v.index + 1}
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
