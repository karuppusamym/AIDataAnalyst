import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const { list, create, submit } = vi.hoisted(() => ({list: vi.fn(), create: vi.fn(), submit: vi.fn()}));
vi.mock("../lib/api/ontology", () => ({listOntologyVersions: list, createOntologyVersion: create, submitOntologyVersion: submit}));
import { OntologyManager } from "./OntologyManager";
const definition = {name: "Customer", owner: "steward", provenance: "approved glossary", concepts: [{key: "customer", name: "Customer", description: "A customer"}], relations: [], mappings: []};
beforeEach(() => {list.mockReset().mockResolvedValue([]); create.mockReset(); submit.mockReset();});
it("saves a definition as a draft without submitting or publishing", async () => {
  create.mockResolvedValue({id: "v1", status: "DRAFT"});
  render(<OntologyManager organizationId="org" onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Ontology definition JSON"), {target: {value: JSON.stringify(definition)}});
  fireEvent.click(screen.getByRole("button", {name: "Save ontology draft"}));
  await screen.findByText("Ontology draft saved; nothing published.");
  expect(create).toHaveBeenCalledWith("org", {ontology_key: "customer", base_version: 0, definition});
  expect(submit).not.toHaveBeenCalled();
});
it("refuses invalid JSON locally and preserves the editor", async () => {
  render(<OntologyManager organizationId="org" onClose={vi.fn()} />);
  await waitFor(() => expect(list).toHaveBeenCalled());
  fireEvent.change(screen.getByLabelText("Ontology definition JSON"), {target: {value: "not json"}});
  fireEvent.click(screen.getByRole("button", {name: "Save ontology draft"}));
  await screen.findByRole("alert");
  expect(create).not.toHaveBeenCalled();
  expect(screen.getByLabelText("Ontology definition JSON")).toHaveValue("not json");
});
it("preserves the selected historical baseline instead of silently claiming the latest version", async () => {
  list.mockResolvedValue([{id: "v1", ontology_key: "customer", version: 1, base_version: 0, published_version: 3, status: "APPROVED", definition}]);
  render(<OntologyManager organizationId="org" onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "Use as new draft"}));
  expect(screen.getByLabelText("Published base version")).toHaveValue(1);
});
it("submits a draft explicitly and points to independent review", async () => {
  list.mockResolvedValue([{id: "v1", ontology_key: "customer", version: 1, base_version: 0, published_version: 0, status: "DRAFT", definition}]);
  submit.mockResolvedValue({id: "v1", status: "PENDING_APPROVAL"});
  render(<OntologyManager organizationId="org" onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", {name: "Submit ontology v1 for review"}));
  await screen.findByText("Submitted to the review queue; an independent reviewer must decide.");
  expect(submit).toHaveBeenCalledWith("v1");
});
