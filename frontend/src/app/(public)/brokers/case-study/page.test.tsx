/** Genuine evidence case study — shows the real redacted facts, clearly marked REAL and separate from synthetic. */
import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

import EvidenceCaseStudyPage from "./page";

afterEach(() => cleanup());

describe("EvidenceCaseStudyPage", () => {
  it("renders the genuine facts, the REAL badge, and the redaction note", () => {
    render(<EvidenceCaseStudyPage />);
    expect(screen.getByTestId("case-study")).toBeTruthy();
    expect(screen.getByText(/REAL · AUTHENTICATED ON PRODUCTION/i)).toBeTruthy();
    expect(screen.getByText("24.12 USD")).toBeTruthy();            // the genuine amount (not the synthetic $250)
    expect(screen.getByText(/33m01s/)).toBeTruthy();
    expect(screen.getByText("Ethereum")).toBeTruthy();
    expect(screen.getByText(/No raw email, account number, confirmation link/i)).toBeTruthy();
  });
});
