import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HomeScreen } from "./HomeScreen";

describe("HomeScreen", () => {
  it("turns the landing page into a data-backed overview", async () => {
    render(<HomeScreen persona="Steward" onNavigate={() => undefined} />);

    expect(screen.getByRole("heading", { name: /Find the right data/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Recently updated assets" })).toBeInTheDocument();
    expect(await screen.findByText("1,000,000")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("Steward setup")).toBeInTheDocument();
  });

  it("opens real product areas from its primary actions", () => {
    const onNavigate = vi.fn();
    render(<HomeScreen persona="Analyst" onNavigate={onNavigate} />);

    screen.getByRole("button", { name: "Explore catalog" }).click();
    screen.getByRole("button", { name: "Ask Atlas" }).click();

    expect(onNavigate).toHaveBeenNthCalledWith(1, "catalog");
    expect(onNavigate).toHaveBeenNthCalledWith(2, "analyst");
  });
});
