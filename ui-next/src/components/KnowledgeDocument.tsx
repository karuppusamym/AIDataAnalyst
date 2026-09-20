import type { ReactNode } from "react";
import "./Knowledge.css";

/* ---------------------------------------------------------------------------
   One stored OKF document, rendered readable -- and inert (R11-OKF02).

   The bundle is Markdown the server already refused to publish if it carried
   raw HTML, an external link or a fenced code block. This renderer does not
   rely on that: it never hands a string to `innerHTML`. It recognises the
   handful of constructs the Atlas renderer emits -- headings, bullet lists,
   pipe tables, footnote definitions, `code` spans, **bold** and bundle-relative
   links -- and builds React elements for them; anything else is text, so a
   `<script>` in an approved description would appear as the characters
   `<script>`, never as markup.

   Bundle links -- absolute (`/sources/.../view-<key>.md`), relative as an
   index writes them (`tables/table-<key>.md`), or a directory's index --
   become buttons that open that document in the same view: the wiki reading
   the design asks for ("render the same approved content as the wiki"), over
   the stored bytes. Anything that is not a bundle path stays plain text.

   The frontmatter is not shown raw. Its `type`, `status` and `generated.at`
   are the three facts a reader needs, and they are pulled out as labels; the
   full frontmatter travels with the downloaded file.
--------------------------------------------------------------------------- */

export interface KnowledgeFrontmatter {
  type: string | null;
  status: string | null;
  generatedAt: string | null;
}

export function splitDocument(content: string): { frontmatter: KnowledgeFrontmatter; body: string } {
  const empty: KnowledgeFrontmatter = { type: null, status: null, generatedAt: null };
  if (!content.startsWith("---\n")) return { frontmatter: empty, body: content };
  const end = content.indexOf("\n---\n", 4);
  if (end < 0) return { frontmatter: empty, body: content };
  const raw = content.slice(4, end);
  const body = content.slice(end + 5);
  const scalar = (key: string): string | null => {
    const match = raw.match(new RegExp(`^${key}:\\s*(.+)$`, "m"));
    return match ? (match[1] ?? "").replace(/^['"]|['"]$/g, "").trim() : null;
  };
  const generated = raw.match(/^generated:\n(?:\s+.+\n?)*/m)?.[0] ?? "";
  const at = generated.match(/^\s+at:\s*['"]?([^'"\n]+)['"]?$/m)?.[1] ?? null;
  return {
    frontmatter: { type: scalar("type"), status: scalar("status"), generatedAt: at },
    body,
  };
}

const INLINE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)\s]+\))|(\[\^[^\]]+\])|((?<![\w])_[^_\n]+_(?![\w]))/g;

/** A link target as a bundle path, or null for anything that is not one.
 *
 *  Absolute targets (`/sources/...`) are bundle-rooted, as spec §6.1
 *  recommends; relative ones (an index's `tables/table-<key>.md`) resolve
 *  against the directory of the document they appear in; a directory target
 *  (`schemas/schema-<key>/`) means its `index.md`. Any scheme -- `https:`,
 *  `mailto:`, `atlas:` -- or a protocol-relative `//host` is not a bundle path
 *  and is never made navigable. */
export function resolveBundleTarget(target: string, basePath: string | null): string | null {
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("//")) return null;
  const parts = target.startsWith("/") ? [] : (basePath ?? "").split("/").slice(0, -1);
  for (const segment of target.replace(/^\//, "").split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (parts.length === 0) return null;
      parts.pop();
      continue;
    }
    parts.push(segment);
  }
  let path = parts.join("/");
  if (target.endsWith("/")) path = path ? `${path}/index.md` : "index.md";
  return path.endsWith(".md") ? path : null;
}

type Navigator = { basePath: string | null; open: (path: string) => void } | null;

function inline(text: string, navigator: Navigator): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const token = match[0];
    const key = `i${index++}`;
    if (match[1]) {
      out.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (match[2]) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (match[3]) {
      const label = token.slice(1, token.indexOf("]("));
      const target = token.slice(token.indexOf("](") + 2, -1);
      const bundlePath = navigator ? resolveBundleTarget(target, navigator.basePath) : null;
      out.push(
        bundlePath && navigator ? (
          <button
            key={key}
            type="button"
            className="kdoc__link"
            onClick={() => navigator.open(bundlePath)}
            title={`Open ${bundlePath}`}
          >
            {label}
          </button>
        ) : (
          <span key={key}>{label}</span>
        ),
      );
    } else if (match[4]) {
      out.push(
        <sup key={key} className="kdoc__fn">
          {token.slice(2, -1)}
        </sup>,
      );
    } else {
      out.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    last = at + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function tableCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split(/(?<!\\)\|/)
    .map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

/** A heading's words without its markdown, for naming what sits under it. */
function plainText(markdown: string): string {
  return markdown
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/`|\*\*/g, "")
    .trim();
}

export function KnowledgeDocument({
  content,
  onNavigate = null,
  path = null,
}: {
  content: string;
  onNavigate?: ((path: string) => void) | null;
  /** The document's own bundle path, against which relative links resolve. */
  path?: string | null;
}) {
  const navigator: Navigator = onNavigate ? { basePath: path, open: onNavigate } : null;
  const { frontmatter, body } = splitDocument(content);
  const blocks: ReactNode[] = [];
  const lines = body.split("\n");
  const at = (n: number): string => lines[n] ?? "";
  let i = 0;
  let key = 0;
  let section = "";
  const tableNames = new Map<string, number>();
  while (i < lines.length) {
    const line = at(i);
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      const level = (heading[1] ?? "#").length;
      const text = heading[2] ?? "";
      section = plainText(text);
      blocks.push(
        level === 1 ? (
          <h4 key={key++} className="kdoc__h">
            {inline(text, navigator)}
          </h4>
        ) : (
          <h5 key={key++} className="kdoc__h kdoc__h--sub">
            {inline(text, navigator)}
          </h5>
        ),
      );
      i += 1;
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && at(i).startsWith("|")) {
        rows.push(at(i));
        i += 1;
      }
      const [head = "", , ...rest] = rows;
      // A table wider than its pane scrolls sideways instead of breaking its words, so the
      // scroll area must be reachable from the keyboard and say what it holds; two tables
      // under one heading are told apart by number.
      const base = section ? `${section} table` : "Table";
      const seen = (tableNames.get(base) ?? 0) + 1;
      tableNames.set(base, seen);
      blocks.push(
        <div
          key={key++}
          className="kdoc__tablewrap"
          tabIndex={0}
          role="group"
          aria-label={`${seen === 1 ? base : `${base} ${seen}`}, scrollable`}
        >
          <table className="kdoc__table">
            <thead>
              <tr>
                {tableCells(head).map((cell, c) => (
                  <th key={c}>{inline(cell, navigator)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rest.map((row, r) => (
                <tr key={r}>
                  {tableCells(row).map((cell, c) => (
                    <td key={c}>{inline(cell, navigator)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*[*-]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[*-]\s+/.test(at(i))) {
        items.push(at(i).replace(/^\s*[*-]\s+/, ""));
        i += 1;
      }
      blocks.push(
        <ul key={key++} className="kdoc__list">
          {items.map((item, n) => (
            <li key={n}>{inline(item, navigator)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    const footnote = line.match(/^\[\^([^\]]+)\]:\s*(.*)$/);
    if (footnote) {
      blocks.push(
        <p key={key++} className="kdoc__note">
          <sup>{footnote[1]}</sup> {inline(footnote[2] ?? "", navigator)}
        </p>,
      );
      i += 1;
      continue;
    }
    // The first line is always consumed, so a line no other branch claims can never stall
    // the loop; the rest of the paragraph runs until a blank line or another block starts.
    const paragraph: string[] = [line];
    i += 1;
    while (
      i < lines.length &&
      at(i).trim() &&
      !/^(#{1,3}\s|\||\s*[*-]\s+|\[\^[^\]]+\]:)/.test(at(i))
    ) {
      paragraph.push(at(i));
      i += 1;
    }
    blocks.push(
      <p key={key++} className="kdoc__p">
        {inline(paragraph.join(" "), navigator)}
      </p>,
    );
  }
  return (
    <article className="kdoc" aria-label="Knowledge document">
      {frontmatter.type ? (
        <div className="kdoc__meta">
          <span className="kdoc__type">{frontmatter.type}</span>
          {frontmatter.status ? <span className="kdoc__status">{frontmatter.status}</span> : null}
          {frontmatter.generatedAt ? (
            <span className="kdoc__at">content as of {frontmatter.generatedAt}</span>
          ) : null}
        </div>
      ) : null}
      {blocks}
    </article>
  );
}
