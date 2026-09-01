import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PersonaNav } from "./PersonaNav";

/* ---------------------------------------------------------------------------
   UX-1: "Browser selection removed in production" (tracker exit criterion).

   These assert the rendered UI state directly -- no `<select>` may exist in
   OIDC/production mode, and the manual switcher must still exist in
   development mode -- rather than asserting on internal component state,
   since a hidden-but-present control would still be a fake one.
--------------------------------------------------------------------------- */

describe("PersonaNav in OIDC (production) mode", () => {
  it("renders no persona switcher — the server-derived persona is not user-selectable", () => {
    render(<PersonaNav identityProvider="OIDC" persona="Steward" />);

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByTestId("persona-select")).not.toBeInTheDocument();
  });

  it("displays the server-derived persona as read-only text", () => {
    render(<PersonaNav identityProvider="OIDC" persona="Auditor" />);

    expect(screen.getByTestId("persona-value")).toHaveTextContent("Auditor");
  });

  it("shows an explicit no-mapping message rather than a blank or a picker when OIDC groups map to no persona", () => {
    render(<PersonaNav identityProvider="OIDC" persona={null} />);

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByTestId("persona-value")).toHaveTextContent(
      "No persona mapped for your groups",
    );
  });

  it("ignores onPersonaChange entirely — there is no control to fire it", () => {
    const onPersonaChange = vi.fn();
    render(<PersonaNav identityProvider="OIDC" persona="Steward" onPersonaChange={onPersonaChange} />);

    expect(onPersonaChange).not.toHaveBeenCalled();
  });
});

describe("PersonaNav in development mode", () => {
  it("renders the manual persona switcher", () => {
    render(<PersonaNav identityProvider="DEVELOPMENT" persona="Steward" />);

    expect(screen.getByTestId("persona-select")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("labels the switcher as a dev-only convenience", () => {
    render(<PersonaNav identityProvider="DEVELOPMENT" persona="Steward" />);

    expect(screen.getByText(/dev only/i)).toBeInTheDocument();
  });

  it("lets the developer pick any of the portal's personas", () => {
    render(<PersonaNav identityProvider="DEVELOPMENT" persona="Steward" />);

    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Analyst", "Steward", "Reviewer", "Operator", "Auditor"]);
  });

  it("calls onPersonaChange when the developer picks a different persona", () => {
    const onPersonaChange = vi.fn();
    render(
      <PersonaNav identityProvider="DEVELOPMENT" persona="Steward" onPersonaChange={onPersonaChange} />,
    );

    fireEvent.change(screen.getByTestId("persona-select"), { target: { value: "Auditor" } });

    expect(onPersonaChange).toHaveBeenCalledWith("Auditor");
  });
});

describe("PersonaNav while the identity mode is not yet known", () => {
  it("renders nothing — fails closed rather than guessing a mode", () => {
    render(<PersonaNav identityProvider={null} persona={null} />);

    expect(screen.queryByTestId("persona-nav")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});
