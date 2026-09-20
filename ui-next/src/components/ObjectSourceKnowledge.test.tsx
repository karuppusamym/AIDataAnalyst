import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { OkfDocumentRead, OkfPublicationRead } from "../lib/types";
import type { ObjectKnowledgeRead, ObjectKnowledgeSource } from "../lib/api/knowledge";
import { ApiError } from "../lib/http";
import sourcesScreenSource from "../screens/SourcesScreen.tsx?raw";
import { expectNoAxeViolations } from "../test/a11y";

/* ---------------------------------------------------------------------------
   R11-OKF02. The Catalog object view, when no context product holds the object.

   The server answers from the object's own datasource bundle (`source` on
   `GET /v1/metadata/tables/{id}/okf-knowledge`), because a document's path is a
   digest of catalog, schema and name that no table read exposes. What these
   tests hold in place:

     - the three states are told apart by what they SAY: a document is shown; an
       absence is said as an absence; and a refusal is an alert carrying the
       server's reason code -- never the quiet "no document" an absence gets;
     - it is ONE request: this view never asks a source route for a path it cannot
       form, so a refusal and an absence are exactly the server's two answers;
     - a product's reading wins: with any product entry the source is not shown;
     - the words about what a source bundle holds are the Sources screen's own;
     - every state has no axe-detectable WCAG A/AA violation.
--------------------------------------------------------------------------- */

const fetchObjectKnowledge =
  vi.fn<(tableId: string, signal?: AbortSignal) => Promise<ObjectKnowledgeRead>>();
const downloadOkfBundle = vi.fn<(versionId: string, publicationId: string) => Promise<void>>();
const downloadSourceOkfBundle = vi.fn<(datasourceId: string, publicationId: string) => Promise<void>>();
/* Every route a client could use to find the document itself. None may be called. */
const fetchSourceOkfBundle = vi.fn();
const fetchSourceOkfDocument = vi.fn();
const fetchSourceOkfPublications = vi.fn();
const selectSourceOkfContext = vi.fn();

vi.mock("../lib/api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api/knowledge")>();
  return {
    ...actual,
    fetchObjectKnowledge: (tableId: string, signal?: AbortSignal) => fetchObjectKnowledge(tableId, signal),
    downloadOkfBundle: (versionId: string, publicationId: string) => downloadOkfBundle(versionId, publicationId),
    downloadSourceOkfBundle: (datasourceId: string, publicationId: string) =>
      downloadSourceOkfBundle(datasourceId, publicationId),
    fetchSourceOkfBundle: (...args: unknown[]) => fetchSourceOkfBundle(...args),
    fetchSourceOkfDocument: (...args: unknown[]) => fetchSourceOkfDocument(...args),
    fetchSourceOkfPublications: (...args: unknown[]) => fetchSourceOkfPublications(...args),
    selectSourceOkfContext: (...args: unknown[]) => selectSourceOkfContext(...args),
  };
});

const { ObjectKnowledge } = await import("./ObjectKnowledge");

const READABLE_ONLY = "Only what you may read of this source is in it, and nothing else is counted.";
const TABLE = "sources/source-aaaa/schemas/schema-bbbb/tables/table-fedcba9876543210fedcba9876543210.md";

function publication(overrides: Partial<OkfPublicationRead> = {}): OkfPublicationRead {
  return {
    publication_id: "pub-2",
    sequence: 2,
    trigger: "SOURCE_CHANGE",
    captured_at: "2026-09-18T01:00:00Z",
    is_current: true,
    bundle_content_digest: "d".repeat(64),
    content_snapshot_digest: "e".repeat(64),
    document_count: 14,
    rendered_count: 8,
    carried_count: 6,
    valid: true,
    changes: { added: [], changed: [], removed: [], changed_subjects: 0, marked_subjects: 0, full_render: false },
    ...overrides,
  };
}

function document(rendered = 2): OkfDocumentRead {
  const content =
    "---\ntype: Atlas Table\nstatus: draft\n---\n\n# Purpose\n\nOne row per order.\n\n" +
    "# Schema\n\n| Column | Type |\n|---|---|\n| `order_id` | `uuid` |\n";
  return {
    publication_id: "pub-2",
    publication_sequence: 2,
    path: TABLE,
    sha256: "9".repeat(64),
    bytes: content.length,
    rendered_in_sequence: rendered,
    subject_key: "fedcba9876543210fedcba9876543210",
    content,
  };
}

function documentState(overrides: { publication?: Partial<OkfPublicationRead>; rendered?: number } = {}): ObjectKnowledgeSource {
  return {
    state: "DOCUMENT",
    datasource_id: "ds-1",
    datasource_name: "Customer Master (Postgres, sample)",
    publication: publication(overrides.publication),
    document: document(overrides.rendered),
    coverage: {
      key: "fedcba9876543210fedcba9876543210",
      description_state: "APPROVED",
      definition_digest: "abcdef0123456789abcdef",
      definition_capture_version: 3,
    },
  };
}

const ABSENT: ObjectKnowledgeSource = {
  state: "NOT_IN_BUNDLE",
  datasource_id: "ds-1",
  datasource_name: "Customer Master (Postgres, sample)",
};
const REFUSED: ObjectKnowledgeSource = { state: "REFUSED", reason: "NO_BINDING_FOR_DATASOURCE" };

async function open(source: ObjectKnowledgeSource | null | undefined, items: ObjectKnowledgeRead["items"] = []) {
  const user = userEvent.setup();
  fetchObjectKnowledge.mockResolvedValue({ table_id: "t-1", items, ...(source === undefined ? {} : { source }) });
  const view = render(<ObjectKnowledge tableId="t-1" />);
  await user.click(screen.getByRole("button", { name: /Knowledge/ }));
  return { user, ...view };
}

beforeEach(() => {
  for (const fn of [
    fetchObjectKnowledge,
    downloadOkfBundle,
    downloadSourceOkfBundle,
    fetchSourceOkfBundle,
    fetchSourceOkfDocument,
    fetchSourceOkfPublications,
    selectSourceOkfContext,
  ]) {
    fn.mockReset();
  }
  downloadSourceOkfBundle.mockResolvedValue(undefined);
});

describe("ObjectKnowledge over the data source's own bundle", () => {
  it("shows the object's document from the source bundle when no product holds it", async () => {
    await open(documentState());
    expect(await screen.findByText("Customer Master (Postgres, sample) · data source bundle")).toBeInTheDocument();
    expect(screen.getByText("One row per order.")).toBeInTheDocument();
    expect(screen.getByText("publication 2")).toBeInTheDocument();
    expect(screen.getByText("changed in this publication")).toBeInTheDocument();
    expect(screen.getByText("description approved")).toBeInTheDocument();
    expect(screen.getByText("definition abcdef012345")).toBeInTheDocument();
    expect(screen.getByText("capture v3")).toBeInTheDocument();
    expect(screen.getByText(/No context product you may read includes this object\. This is its document/)).toBeInTheDocument();
    // The Sources panel's own sentence, so the two surfaces make one claim.
    expect(screen.getByText(READABLE_ONLY)).toBeInTheDocument();
    // It is a document, not a refusal and not an absence.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/holds no document for it/)).toBeNull();
  });

  it("says how long a document has been unchanged when a rebuild carried it", async () => {
    await open(documentState({ rendered: 1 }));
    expect(await screen.findByText("unchanged since publication 1")).toBeInTheDocument();
  });

  it("downloads the source bundle publication the document belongs to, and no product's", async () => {
    const { user } = await open(documentState());
    await user.click(await screen.findByRole("button", { name: "Download bundle" }));
    expect(downloadSourceOkfBundle).toHaveBeenCalledWith("ds-1", "pub-2");
    expect(downloadOkfBundle).not.toHaveBeenCalled();
  });

  it("does not offer a download the server would refuse", async () => {
    await open(documentState({ publication: { valid: false } }));
    expect(await screen.findByRole("button", { name: "Download bundle" })).toBeDisabled();
  });

  it("reports a failed download as its own alert and keeps the document", async () => {
    downloadSourceOkfBundle.mockRejectedValue(new ApiError(409, "findings"));
    const { user } = await open(documentState());
    await user.click(await screen.findByRole("button", { name: "Download bundle" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The bundle could not be published");
    expect(screen.getByText("One row per order.")).toBeInTheDocument();
  });

  it("says a bundle holds no document for the object, as an absence and nothing more", async () => {
    await open(ABSENT);
    expect(
      await screen.findByText(
        "No context product you may read includes this object, and the bundle of Customer Master (Postgres, sample) holds no document for it that you may read.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(READABLE_ONLY)).toBeInTheDocument();
    // An absence is not a refusal and shows no document, no publication and no download.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/not permitted/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Download bundle" })).toBeNull();
    expect(screen.queryByText(/publication \d/)).toBeNull();
  });

  it("shows a refusal as a refusal with the server's reason code, never as no document", async () => {
    await open(REFUSED);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You are not permitted to read this object’s data source bundle.");
    expect(within(alert).getByText("NO_BINDING_FOR_DATASOURCE")).toBeInTheDocument();
    // None of the words an absence, or the plain empty state, would use.
    expect(screen.queryByText(/holds no document/)).toBeNull();
    expect(screen.queryByText("No published knowledge bundle you may read includes this object.")).toBeNull();
    expect(screen.queryByText(READABLE_ONLY)).toBeNull();
    expect(screen.queryByRole("button", { name: "Download bundle" })).toBeNull();
  });

  it("does not take an unknown state for an absence", async () => {
    await open({ state: "ARCHIVED" } as unknown as ObjectKnowledgeSource);
    expect(await screen.findByRole("alert")).toHaveTextContent("answered in a way this view cannot show");
    expect(screen.queryByText(/holds no document/)).toBeNull();
  });

  it("keeps the plain empty state when the server sent no source entry at all", async () => {
    await open(undefined);
    expect(await screen.findByText("No published knowledge bundle you may read includes this object.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the plain empty state when the source entry is null", async () => {
    await open(null);
    expect(await screen.findByText("No published knowledge bundle you may read includes this object.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("asks the server once and never asks a source route for the document itself", async () => {
    await open(documentState());
    await screen.findByText("One row per order.");
    expect(fetchObjectKnowledge).toHaveBeenCalledTimes(1);
    for (const route of [fetchSourceOkfBundle, fetchSourceOkfDocument, fetchSourceOkfPublications, selectSourceOkfContext]) {
      expect(route).not.toHaveBeenCalled();
    }
  });

  it("lets a product's reading win: with a product entry the source is not shown", async () => {
    const productItem: ObjectKnowledgeRead["items"][number] = {
      context_product_version_id: "ver-1",
      product_key: "revenue_context",
      product_version: 2,
      product_name: "Revenue context",
      publication: publication(),
      document: document(),
      coverage: { description_state: "NONE", definition_digest: "abcdef0123456789" },
    };
    // Even if a server sent both, the product's reading is what is shown.
    await open(documentState(), [productItem]);
    expect(await screen.findByText("Revenue context · revenue_context v2")).toBeInTheDocument();
    expect(screen.queryByText(/data source bundle/)).toBeNull();
    expect(screen.queryByText(READABLE_ONLY)).toBeNull();
  });
});

describe("the wording about what a source bundle holds", () => {
  it("is the Sources screen's own sentence, word for word", () => {
    expect(sourcesScreenSource).toContain(READABLE_ONLY);
  });
});

describe("accessibility of the source states", () => {
  it("has no WCAG A/AA violation in the document, absence and refusal states", async () => {
    for (const source of [documentState(), ABSENT, REFUSED]) {
      const { container, unmount } = await open(source);
      await screen.findByRole("region", { name: "Knowledge" });
      await screen.findAllByText(/data source|permitted|holds no document/);
      await expectNoAxeViolations(container);
      unmount();
    }
  });
});
