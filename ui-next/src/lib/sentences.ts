/* ---------------------------------------------------------------------------
   Small sentence helpers shared by screens that name roles in prose.
--------------------------------------------------------------------------- */

/** `["A", "B", "C"]` -> "A, B or C". A screen that says which roles a surface needs says it this way. */
export const listOr = (items: readonly string[]): string =>
  items.length < 2 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
