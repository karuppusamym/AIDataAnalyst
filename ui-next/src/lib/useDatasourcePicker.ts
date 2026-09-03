import { useEffect, useState } from "react";
import { fetchOrgDatasources } from "./api";

/** Several screens read data scoped to one datasource (agent-analyses,
 *  relationship-candidate review, business-annotations, quality-summary/
 *  -incidents) and need the same "pick a datasource, see its name" setup
 *  `NarratedLineageScreen` (UX-20) built first. Extracted here rather than
 *  re-copied a fifth time; `NarratedLineageScreen` itself keeps its own
 *  inline version (not touched in passing, matching this codebase's existing
 *  convention of leaving already-shipped screens as they landed). */
export function useDatasourcePicker(organizationId: string) {
  const [datasources, setDatasources] = useState<{ id: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchOrgDatasources(organizationId)
      .then((page) => {
        if (cancelled) return;
        setDatasources(page.items.map((d) => ({ id: d.id, name: d.name })));
      })
      .catch(() => {
        // Degrades to an empty picker -- a caller that already has a
        // datasource id in the URL keeps working; only the dropdown's
        // option list is affected.
        if (!cancelled) setError("Could not load the datasource list.");
      });
    return () => {
      cancelled = true;
    };
  }, [organizationId]);

  return { datasources, error } as const;
}

export function datasourceName(
  datasources: readonly { id: string; name: string }[],
  id: string | null,
): string | null {
  return datasources.find((d) => d.id === id)?.name ?? null;
}
