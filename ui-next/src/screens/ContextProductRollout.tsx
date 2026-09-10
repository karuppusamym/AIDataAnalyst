import { useCallback, useEffect, useState } from "react";
import type {
  ContextProductConsumerBindingRead,
  ContextProductRead,
  ContextProductVersionRead,
} from "../lib/types";
import {
  fetchContextProductBindings,
  fetchContextProductVersions,
  removeContextProductBinding,
  setContextProductBinding,
} from "../lib/api";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { LoadingPanel, useAsyncResource } from "../components/screenState";
import type { StatusChannel } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Rollout -- the AT-7(b) consumer-binding registry.

   Publishing a version makes it available to every principal holding an
   allowed consumer role. A binding is the staged-rollout control on top of
   that: it pins one *named* consumer (usually an agent's service principal)
   to one specific version, so a new version can be proven against one agent
   before every agent moves. Unpinning returns that consumer to the published
   version; it never grants or revokes access, which stays governed by
   `allowed_consumer_roles`.

   Both endpoints shipped with the API and had no client at all, which is why
   a Context Product could be compiled but not actually operated.

   The controller ships with the panel because the two reads and the two
   writes are meaningless apart: a binding names a version, so the version
   list is not "extra context" for the pin form, it is the set of legal
   values. Both reads therefore succeed or fail together, and every write
   re-reads both -- pinning a consumer to a version that has since been
   deprecated must show that immediately, not at the next visit.
--------------------------------------------------------------------------- */

interface Rollout {
  readonly versions: ContextProductVersionRead[];
  readonly bindings: ContextProductConsumerBindingRead[];
}

const NO_ROLLOUT: Rollout = { versions: [], bindings: [] };

export function useRollout(product: ContextProductRead | null, channel: StatusChannel) {
  const productId = product?.id ?? "";
  const resource = useAsyncResource<Rollout>(
    async (signal) => {
      // A failure in either read is one failure of "the rollout", not half a
      // panel: a version list with no bindings reads as "nobody is pinned".
      const [versionPage, bindingPage] = await Promise.all([
        fetchContextProductVersions(productId, { limit: 200 }, signal),
        fetchContextProductBindings(productId, { limit: 200 }, signal),
      ]);
      return { versions: versionPage.items, bindings: bindingPage.items };
    },
    [productId],
    { enabled: Boolean(productId) },
  );

  const [busyConsumer, setBusyConsumer] = useState<string | null>(null);
  const reload = resource.reload;

  const write = useCallback(
    async (consumerPrincipalId: string, perform: () => Promise<unknown>, done: string) => {
      setBusyConsumer(consumerPrincipalId);
      try {
        await perform();
        channel.success(done);
        reload();
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setBusyConsumer(null);
      }
    },
    [channel, reload],
  );

  const bind = useCallback(
    (consumerPrincipalId: string, versionId: string) =>
      void write(
        consumerPrincipalId,
        () => setContextProductBinding(productId, consumerPrincipalId, versionId),
        `${consumerPrincipalId} is pinned. Every other consumer stays on the published version.`,
      ),
    [productId, write],
  );

  const unbind = useCallback(
    (consumerPrincipalId: string) =>
      void write(
        consumerPrincipalId,
        () => removeContextProductBinding(productId, consumerPrincipalId),
        `${consumerPrincipalId} now resolves to the published version.`,
      ),
    [productId, write],
  );

  return { resource, busyConsumer, bind, unbind } as const;
}

export function RolloutPanel({
  product,
  versions,
  bindings,
  loading,
  error,
  busyConsumer,
  onBind,
  onUnbind,
  onReload,
  onClose,
}: {
  product: ContextProductRead;
  versions: ContextProductVersionRead[];
  bindings: ContextProductConsumerBindingRead[];
  loading: boolean;
  error: string | null;
  busyConsumer: string | null;
  onBind: (consumerPrincipalId: string, versionId: string) => void;
  onUnbind: (consumerPrincipalId: string) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const [consumer, setConsumer] = useState("");
  const [versionId, setVersionId] = useState("");

  // Default the version picker to the published version -- the one a new
  // consumer would resolve to anyway -- so the common case is one field.
  useEffect(() => {
    if (versionId && versions.some((v) => v.id === versionId)) return;
    const published = versions.find((v) => v.status === "PUBLISHED") ?? versions[0];
    setVersionId(published?.id ?? "");
  }, [versions, versionId]);

  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = consumer.trim();
    if (!trimmed || !versionId) return;
    onBind(trimmed, versionId);
    setConsumer("");
  };

  return (
    <article className="cprollout" aria-label={`Rollout for ${product.product_key}`}>
      <header className="cprollout__head">
        <div>
          <p className="cprollout__eyebrow">STAGED ROLLOUT</p>
          <h2 className="cprollout__h2">{product.product_key}</h2>
          <p className="cprollout__lede">
            Pin a named consumer to one version. Anyone not pinned here resolves to the published version.
          </p>
        </div>
        <div className="cprollout__headactions">
          <Button onClick={onReload}>Refresh</Button>
          <Button onClick={onClose}>Close</Button>
        </div>
      </header>

      {error ? (
        <ErrorState title="Rollout could not be loaded" detail={error} onRetry={onReload} />
      ) : loading ? (
        <LoadingPanel label="Loading rollout…" />
      ) : (
        <>
          <form className="cprollout__form" onSubmit={submit}>
            <Field label="Consumer principal">
              <input
                required
                placeholder="risk-copilot@agents.tenant.example"
                value={consumer}
                onChange={(e) => setConsumer(e.target.value)}
              />
            </Field>
            <Field label="Bound version">
              <select value={versionId} onChange={(e) => setVersionId(e.target.value)}>
                {versions.map((v) => (
                  <option key={v.id} value={v.id}>
                    v{v.version} · {v.status.toLowerCase().replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </Field>
            <Button type="submit" variant="primary" disabled={busyConsumer !== null || versions.length === 0}>
              {busyConsumer !== null ? "Saving…" : "Pin consumer"}
            </Button>
          </form>

          {bindings.length === 0 ? (
            <Empty
              title="No pinned consumers"
              hint="Every eligible consumer resolves to the published version. Pin one to stage a new version against it first."
            />
          ) : (
            <table className="cprollout__table">
              <caption className="cprollout__caption">
                {bindings.length} pinned consumer{bindings.length === 1 ? "" : "s"}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Consumer</th>
                  <th scope="col">Version</th>
                  <th scope="col">Pinned</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {bindings.map((b) => (
                  <tr key={b.id}>
                    <td>
                      <code>{b.consumer_principal_id}</code>
                    </td>
                    <td>
                      <Pill tone="info">v{b.bound_version_number}</Pill>
                    </td>
                    <td>{new Date(b.updated_at).toLocaleDateString()}</td>
                    <td className="cprollout__rowaction">
                      <Button disabled={busyConsumer !== null} onClick={() => onUnbind(b.consumer_principal_id)}>
                        {busyConsumer === b.consumer_principal_id ? "Removing…" : "Unpin"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </article>
  );
}

export { NO_ROLLOUT };
