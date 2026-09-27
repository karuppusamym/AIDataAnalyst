import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const executeGraphQL = vi.fn();

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, executeGraphQL: (...args: unknown[]) => executeGraphQL(...args) };
});

vi.mock("../lib/appConfig", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/appConfig")>();
  return { ...actual, USE_FIXTURES: false };
});

import { GraphqlExplorer } from "./GraphqlExplorer";

beforeEach(() => executeGraphQL.mockReset());

describe("GraphqlExplorer", () => {
  it("runs a named metadata query with parsed variables", async () => {
    executeGraphQL.mockResolvedValue({ data: { datasources: { totalCount: 2 } } });
    render(<GraphqlExplorer projectId="proj-core" />);

    fireEvent.click(screen.getByRole("button", { name: "Run operation" }));

    await waitFor(() => expect(executeGraphQL).toHaveBeenCalledTimes(1));
    expect(executeGraphQL).toHaveBeenCalledWith({
      operationName: "ListDataSources",
      query: expect.stringContaining("query ListDataSources"),
      variables: { first: 20 },
    });
    expect(await screen.findByText(/"totalCount": 2/)).toBeInTheDocument();
  });

  it("prefills the selected project in the context-product example", () => {
    render(<GraphqlExplorer projectId="proj-core" />);

    fireEvent.change(screen.getByLabelText("Example"), { target: { value: "context-products" } });

    // jest-dom's toHaveValue does a deep-equal against expectedValue (isEqualWith), not
    // Jest's asymmetric-matcher protocol, so expect.stringContaining() never matches a
    // real string value here -- assert on .value directly instead.
    expect((screen.getByLabelText("GraphQL variables") as HTMLTextAreaElement).value).toContain(
      '"projectId": "proj-core"',
    );
  });

  it("requires an explicit acknowledgement before a mutation can run", async () => {
    executeGraphQL.mockResolvedValue({ data: { executeGovernedTool: { replayed: false } } });
    render(<GraphqlExplorer projectId={null} />);

    fireEvent.change(screen.getByLabelText("Example"), { target: { value: "execute-tool" } });
    const run = screen.getByRole("button", { name: "Run operation" });
    expect(run).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: /Execute this governed tool mutation/ }));
    expect(run).toBeEnabled();
    fireEvent.click(run);

    await waitFor(() => expect(executeGraphQL).toHaveBeenCalledTimes(1));
    expect(executeGraphQL.mock.calls[0]![0]).toEqual(
      expect.objectContaining({ operationName: "ExecuteGovernedTool" }),
    );
  });

  it("blocks malformed variables before making a request", () => {
    render(<GraphqlExplorer projectId={null} />);

    fireEvent.change(screen.getByLabelText("GraphQL variables"), { target: { value: "{" } });

    expect(screen.getByRole("alert")).toHaveTextContent("Variables are not valid JSON");
    expect(screen.getByRole("button", { name: "Run operation" })).toBeDisabled();
    expect(executeGraphQL).not.toHaveBeenCalled();
  });

  it("requires acknowledgement when a fragment precedes a mutation", () => {
    render(<GraphqlExplorer projectId={null} />);
    fireEvent.change(screen.getByLabelText("GraphQL operation"), { target: { value:
      "fragment Receipt on GovernedExecutionReceipt { id }\nmutation Execute($request: ExecuteGovernedToolInput!) { executeGovernedTool(request: $request) { receipt { ...Receipt } } }",
    } });
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Run operation" })).toBeDisabled();
    expect(executeGraphQL).not.toHaveBeenCalled();
  });

  it("requires fresh acknowledgement after execution variables change", () => {
    render(<GraphqlExplorer projectId={null} />);
    fireEvent.change(screen.getByLabelText("Example"), { target: { value: "execute-tool" } });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Run operation" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("GraphQL variables"), {
      target: { value: '{"request":{"toolVersionId":"another-tool"}}' },
    });
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Run operation" })).toBeDisabled();
  });
});
