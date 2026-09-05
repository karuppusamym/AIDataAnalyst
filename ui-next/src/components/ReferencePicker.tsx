import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import "./ReferencePicker.css";

/* ---------------------------------------------------------------------------
   ReferencePicker — pick governed objects by name, not by UUID.

   Written for the Context Products form, which asked a steward to type
   "Comma-separated governed table UUIDs" into four free-text boxes. Nobody
   knows a table's UUID. The only way to fill that form was to open the
   Catalog in a second tab, copy an id, come back, and repeat — and a typo
   produced a 422 from the server with no indication which of the four fields
   was wrong.

   The component is deliberately generic over the option shape: every screen
   that composes governed references (tables, semantic versions, glossary
   terms, tool versions) has a different read model, and all any of them need
   to agree on is "give me an id, a label, and a line of context".

   Selection is held by the caller as an ordered `string[]` of ids, which is
   exactly what the request body wants, so there is no mapping step at submit
   time. Options that are still loading, failed to load, or came back empty
   each say so in the picker rather than silently rendering an empty list that
   reads as "there is nothing to pick".
--------------------------------------------------------------------------- */

export interface PickerOption {
  id: string;
  /** The name a person recognises. */
  label: string;
  /** One line of disambiguating context — a schema, a version, an owner. */
  hint?: string;
  /** Rendered as a small pill. Use for lifecycle status. */
  badge?: string;
}

export function ReferencePicker({
  label,
  options,
  selected,
  onChange,
  loading = false,
  error = null,
  emptyHint,
  searchPlaceholder = "Filter…",
  /** Rows visible before the list scrolls. */
  visibleRows = 6,
}: {
  label: string;
  options: PickerOption[];
  selected: string[];
  onChange: (ids: string[]) => void;
  loading?: boolean;
  error?: string | null;
  emptyHint?: string;
  searchPlaceholder?: string;
  visibleRows?: number;
}) {
  const [query, setQuery] = useState("");
  const listId = useRef(`rp-${Math.random().toString(36).slice(2, 9)}`).current;

  const byId = useMemo(() => new Map(options.map((o) => [o.id, o])), [options]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((o) =>
      `${o.label} ${o.hint ?? ""} ${o.badge ?? ""}`.toLowerCase().includes(needle),
    );
  }, [options, query]);

  const toggle = (id: string) => {
    onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
  };

  return (
    <div className="refpicker">
      <div className="refpicker__head">
        <span className="refpicker__label" id={`${listId}-label`}>
          {label}
        </span>
        <span className="refpicker__count">
          {selected.length === 0 ? "none selected" : `${selected.length} selected`}
        </span>
      </div>

      {selected.length > 0 ? (
        <ul className="refpicker__chips" aria-label={`${label}: selected`}>
          {selected.map((id) => (
            <li key={id} className="refpicker__chip">
              <span>{byId.get(id)?.label ?? id}</span>
              <button
                type="button"
                onClick={() => toggle(id)}
                aria-label={`Remove ${byId.get(id)?.label ?? id}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <input
        className="refpicker__search"
        type="search"
        value={query}
        placeholder={searchPlaceholder}
        aria-label={`Filter ${label}`}
        onChange={(e) => setQuery(e.target.value)}
      />

      <div
        className="refpicker__list"
        role="group"
        aria-labelledby={`${listId}-label`}
        style={{ maxHeight: `${visibleRows * 2.4}rem` }}
      >
        {loading ? (
          <p className="refpicker__note" role="status">
            Loading {label.toLowerCase()}…
          </p>
        ) : error ? (
          <p className="refpicker__note refpicker__note--bad" role="alert">
            {error}
          </p>
        ) : options.length === 0 ? (
          <p className="refpicker__note">{emptyHint ?? `No ${label.toLowerCase()} available.`}</p>
        ) : matches.length === 0 ? (
          <p className="refpicker__note">Nothing matches “{query}”.</p>
        ) : (
          matches.map((option) => (
            <label key={option.id} className="refpicker__row">
              <input
                type="checkbox"
                checked={selected.includes(option.id)}
                onChange={() => toggle(option.id)}
              />
              <span className="refpicker__rowmain">
                <span className="refpicker__rowlabel">{option.label}</span>
                {option.hint ? <span className="refpicker__rowhint">{option.hint}</span> : null}
              </span>
              {option.badge ? <span className="refpicker__badge">{option.badge}</span> : null}
            </label>
          ))
        )}
      </div>
    </div>
  );
}

/** Load options once and keep the picker's three display states (loading,
 *  failed, loaded) in one place, so a screen with four pickers does not carry
 *  twelve pieces of state by hand. */
export function usePickerOptions<T>(
  load: (signal: AbortSignal) => Promise<T[]>,
  toOption: (item: T) => PickerOption,
  deps: unknown[],
  { enabled = true }: { enabled?: boolean } = {},
): { options: PickerOption[]; loading: boolean; error: string | null } {
  const [options, setOptions] = useState<PickerOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `toOption` is intentionally not a dependency: callers pass an inline
  // arrow, which would be a new identity on every render and reload the
  // options forever. The mapper is read through a ref so a re-render picks up
  // the current one without re-fetching.
  const map = useRef(toOption);
  map.current = toOption;

  useEffect(() => {
    if (!enabled) {
      setOptions([]);
      setLoading(false);
      setError(null);
      return;
    }
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    load(ac.signal)
      .then((items) => {
        if (ac.signal.aborted) return;
        setOptions(items.map((item) => map.current(item)));
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted || (e as Error)?.name === "AbortError") return;
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      })
      .finally(() => {
        if (!ac.signal.aborted) setLoading(false);
      });
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  return { options, loading, error };
}
