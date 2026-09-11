import { useCallback, useEffect, useState } from "react";
import { Button, Field } from "../components/primitives";
import { ApiError, fetchOrgDatasources } from "../lib/api";
import {
  submitModelImport,
  uploadModelWorkbook,
  type ModelImportBatchRead,
} from "../lib/api/columnDocumentation";
import { requestBlob } from "../lib/api/transport";
import { APP_CONFIG } from "../lib/appConfig";
import { adoptAccessToken, hasAccessToken } from "../lib/authSession";
import { getCurrentOrgId, setCurrentOrgId } from "../lib/org-context";
import type { DataSourceRead } from "../lib/types";
import { ADDIN_AUTH_PATH, parseDialogMessage } from "./authMessage";
import type { WorkbookHost } from "./officeHost";
import { README_SHEET, readWorkbookIdentity, type WorkbookIdentity } from "./workbookIdentity";

/* ---------------------------------------------------------------------------
   Atlas for Excel -- the task pane.

   Save-back is the existing workbook import, reached from inside Excel instead
   of through a browser upload. That is the whole design, and it is why the
   four things a save-back needs were already mostly built:

   - Identity binding. The workbook names its datasource in its README sheet;
     this pane sends the file there, and the server refuses a workbook whose
     README names a different source. The uploader is whoever this pane is
     signed in as -- the development principal, or the OIDC user from the
     sign-in dialog -- recorded by the server, not asserted by the client.
   - Conflict handling. Every editable cell travels with the version it was
     exported against; an edit to something published since is skipped at
     approval, not applied over it.
   - Approval gate. Saving publishes nothing. It creates a draft batch; Submit
     puts it in the review queue; someone other than the submitter decides.
   - Editing integration. This pane: open a model from Atlas as a workbook,
     edit it in Excel, Save to Atlas.
--------------------------------------------------------------------------- */

const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function atlasSourceLink(datasourceId: string): string {
  return `${window.location.origin}/?source=${encodeURIComponent(datasourceId)}#/sources`;
}

type Busy = "identity" | "sign-in" | "save" | "submit" | "sources" | "open" | null;

export function ExcelAddinPane({ host }: { host: WorkbookHost }) {
  const [identity, setIdentity] = useState<WorkbookIdentity | null>(null);
  const [signedIn, setSignedIn] = useState(
    () => APP_CONFIG.authMode !== "oidc" || hasAccessToken(),
  );
  const [busy, setBusy] = useState<Busy>(null);
  const [batch, setBatch] = useState<ModelImportBatchRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [sources, setSources] = useState<DataSourceRead[] | null>(null);
  const [chosenSource, setChosenSource] = useState("");

  const readIdentity = useCallback(async () => {
    setBusy("identity");
    try {
      setIdentity(readWorkbookIdentity(await host.readSheet(README_SHEET)));
    } catch (e) {
      setIdentity(readWorkbookIdentity(null));
      setError(`This workbook could not be read: ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  }, [host]);

  useEffect(() => {
    if (host.inExcel) void readIdentity();
  }, [host, readIdentity]);

  if (!host.inExcel) {
    return (
      <main className="xl">
        <h1 className="xl__h1">Atlas for Excel</h1>
        <p className="xl__lede">
          This page runs inside Excel. Open it from the Atlas button on Excel&apos;s Home tab.
        </p>
      </main>
    );
  }

  const signIn = async () => {
    setError(null);
    if (!APP_CONFIG.oidc) {
      setError("This build has no identity provider configured, so the add-in cannot sign in.");
      return;
    }
    setBusy("sign-in");
    try {
      const reply = parseDialogMessage(
        await host.runDialog(`${window.location.origin}${ADDIN_AUTH_PATH}`),
      );
      if (reply.kind === "error") throw new Error(reply.message);
      adoptAccessToken(reply.token, reply.expiresInSeconds ?? undefined);
      setSignedIn(true);
    } catch (e) {
      setError(`Sign-in did not complete: ${errorText(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!identity?.datasourceId) return;
    setBusy("save");
    setError(null);
    setNotice(null);
    setBatch(null);
    try {
      if (identity.organizationId) setCurrentOrgId(identity.organizationId);
      const bytes = await host.readWorkbookFile();
      const file = new File([bytes], host.fileName(), { type: XLSX_MIME });
      setBatch(await uploadModelWorkbook(identity.datasourceId, file));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const submit = async () => {
    if (!batch) return;
    setBusy("submit");
    setError(null);
    try {
      setBatch(await submitModelImport(batch.id));
      setNotice(
        "Submitted for review. Nothing is published until someone other than you approves it " +
          "in Atlas's review queue.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const loadSources = async () => {
    setBusy("sources");
    setError(null);
    try {
      const page = await fetchOrgDatasources(getCurrentOrgId());
      const items = page.items ?? [];
      setSources(items);
      setChosenSource((current) => current || items[0]?.id || "");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const openModel = async () => {
    if (!chosenSource) return;
    setBusy("open");
    setError(null);
    setNotice(null);
    try {
      const { blob } = await requestBlob(
        `/v1/datasources/${encodeURIComponent(chosenSource)}/model/export.xlsx`,
      );
      await host.openWorkbook(new Uint8Array(await blob.arrayBuffer()));
      setNotice(
        "Opened in a new Excel window. Edit business_description there, then use the Atlas " +
          "button in that window to save it back.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const describedSource = identity?.datasourceName ?? "this workbook's source";

  return (
    <main className="xl">
      <h1 className="xl__h1">Atlas for Excel</h1>

      {!signedIn ? (
        <section className="xl__card" aria-label="Sign in">
          <p>Sign in to Atlas to save this workbook back for review.</p>
          <Button variant="primary" disabled={busy !== null} onClick={() => void signIn()}>
            {busy === "sign-in" ? "Signing in…" : "Sign in to Atlas"}
          </Button>
        </section>
      ) : APP_CONFIG.authMode === "development" ? (
        <p className="xl__meta">{`Development sign-in as ${APP_CONFIG.devPrincipalId}.`}</p>
      ) : null}

      {error ? (
        <div className="xl__error" role="alert">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="xl__notice" role="status">
          {notice}
        </div>
      ) : null}

      {identity === null ? (
        <p className="xl__meta" role="status">
          Reading this workbook…
        </p>
      ) : identity.datasourceId ? (
        <section className="xl__card" aria-label="Save to Atlas">
          <h2 className="xl__h2">{`Save back to ${describedSource}`}</h2>
          <p className="xl__lede">
            Sends this workbook as it is open now. Saving publishes nothing: it prepares a batch you
            can check, then submit for someone else to review. An edit to anything published since
            you opened this workbook is skipped at approval rather than written over it.
          </p>
          <Button
            variant="primary"
            disabled={busy !== null || !signedIn}
            onClick={() => void save()}
          >
            {busy === "save" ? "Saving…" : "Save to Atlas"}
          </Button>

          {batch ? (
            <div className="xl__result" aria-label="Save result">
              <p>
                <b>{plural(batch.change_count, "change")}</b> ready for review
                {batch.rejected_row_count > 0 ? (
                  <>
                    {" · "}
                    <b>{plural(batch.rejected_row_count, "row")}</b> could not be applied
                  </>
                ) : null}
              </p>
              {batch.status === "DRAFT" && batch.change_count === 0 ? (
                <p className="xl__meta">Nothing in this workbook differs from Atlas.</p>
              ) : null}
              {batch.status === "DRAFT" && batch.change_count > 0 ? (
                <Button variant="primary" disabled={busy !== null} onClick={() => void submit()}>
                  {busy === "submit" ? "Submitting…" : "Submit for review"}
                </Button>
              ) : null}
              <a
                className="xl__link"
                href={atlasSourceLink(identity.datasourceId)}
                target="_blank"
                rel="noreferrer"
              >
                Check the changes row by row in Atlas
              </a>
            </div>
          ) : null}
        </section>
      ) : (
        <section className="xl__card" aria-label="Not an Atlas workbook">
          <p>
            This workbook did not come from Atlas, so there is nowhere to save it back to. Open a
            model workbook from Atlas below and edit that one.
          </p>
        </section>
      )}

      <section className="xl__card" aria-label="Open a model from Atlas">
        <h2 className="xl__h2">Open a model from Atlas</h2>
        {sources === null ? (
          <Button disabled={busy !== null || !signedIn} onClick={() => void loadSources()}>
            {busy === "sources" ? "Loading sources…" : "Choose a source"}
          </Button>
        ) : sources.length === 0 ? (
          <p className="xl__meta">No sources are available to you in this organization.</p>
        ) : (
          <>
            <Field label="Source">
              <select
                value={chosenSource}
                onChange={(event) => setChosenSource(event.target.value)}
              >
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.name}
                  </option>
                ))}
              </select>
            </Field>
            <Button disabled={busy !== null || !chosenSource} onClick={() => void openModel()}>
              {busy === "open" ? "Opening…" : "Open in Excel"}
            </Button>
          </>
        )}
      </section>
    </main>
  );
}
