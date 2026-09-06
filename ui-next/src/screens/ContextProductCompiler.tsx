import { useCallback, useState } from "react";
import type { ContextCompilationRead } from "../lib/types";
import { compileContextProductVersion, downloadCompiledContextProduct } from "../lib/api";
import { Button, Empty, Field, Pill } from "../components/primitives";
import type { StatusChannel } from "../components/screenState";

/* ---------------------------------------------------------------------------
   Deterministic delivery: one immutable version compiled for one target.

   Compiling and downloading are one unit because the file is the point. The
   panel shows the artifact and its two hashes so a reviewer can check that
   recompiling the same version produces the same bytes; the download hands
   over the file an agent developer actually installs. Reading an artifact on
   screen is not the same as having it, and a download that cannot say which
   version it came from is not evidence of anything -- which is why the
   version id is read back out of the compilation's own `generated_from`
   rather than remembered from the click that produced it.
--------------------------------------------------------------------------- */

const COMPILE_TARGETS = [
  "MCP",
  "REST",
  "YAML",
  "OSI",
  "ODCS",
  "SNOWFLAKE_SEMANTIC_VIEW",
  "DATABRICKS_METRIC_VIEW",
] as const;

export function useCompiler(channel: StatusChannel) {
  const [target, setTarget] = useState<string>("MCP");
  const [result, setResult] = useState<ContextCompilationRead | null>(null);
  // Which version is compiling, not merely that one is: the registry row that
  // started it is the row whose buttons must be blocked.
  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const compile = useCallback(
    async (versionId: string) => {
      setBusyVersionId(versionId);
      channel.info(`Compiling deterministic ${target} artifact...`);
      try {
        setResult(await compileContextProductVersion(versionId, target));
        channel.success(
          "Artifact compiled. Repeating this request against the same version produces the same hash.",
        );
      } catch (reason) {
        channel.failure(reason);
      } finally {
        setBusyVersionId(null);
      }
    },
    [target, channel],
  );

  const download = useCallback(async () => {
    if (!result) return;
    const versionId = result.generated_from?.context_product_version_id;
    if (typeof versionId !== "string") {
      channel.failure("This artifact does not name the version it came from; recompile it.");
      return;
    }
    setDownloading(true);
    try {
      await downloadCompiledContextProduct(versionId, result.target);
      channel.success("Artifact saved. Ship it alongside your agent's client configuration.");
    } catch (reason) {
      channel.failure(reason);
    } finally {
      setDownloading(false);
    }
  }, [result, channel]);

  return {
    target,
    setTarget,
    result,
    busyVersionId,
    compiling: busyVersionId !== null,
    downloading,
    compile,
    download,
  } as const;
}

export function CompilerPanel({
  target,
  onTargetChange,
  result,
  compiling,
  onDownload,
  downloading,
}: {
  target: string;
  onTargetChange: (t: string) => void;
  result: ContextCompilationRead | null;
  compiling: boolean;
  onDownload: () => void;
  downloading: boolean;
}) {
  return (
    <article className="cpcompiler">
      <header className="cpcompiler__head">
        <div>
          <p className="cpcompiler__eyebrow">DETERMINISTIC DELIVERY</p>
          <h2 className="cpcompiler__h2">Context compiler</h2>
          <p className="cpcompiler__lede">
            Compile one immutable version for MCP, REST, YAML, OSI, ODCS, Snowflake, or Databricks.
          </p>
        </div>
        <Field label="Target">
          <select value={target} onChange={(e) => onTargetChange(e.target.value)}>
            {COMPILE_TARGETS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
      </header>
      <div className="cpcompiler__output">
        {compiling ? (
          <div className="cpcompiler__hint" role="status">
            Compiling…
          </div>
        ) : result ? (
          <>
            <div className="cpcompiler__meta">
              <Pill tone="info">{result.target}</Pill>
              <code>artifact {result.artifact_hash.slice(0, 16)}</code>
              <code>source {result.source_fingerprint.slice(0, 16)}</code>
              <Button onClick={onDownload} disabled={downloading}>
                {downloading ? "Preparing…" : "Download artifact"}
              </Button>
            </div>
            <pre className="cpcompiler__pre">{result.content}</pre>
          </>
        ) : (
          <Empty
            title="Select Compile on a product version"
            hint="The generated artifact and stable hash will appear here."
          />
        )}
      </div>
    </article>
  );
}
