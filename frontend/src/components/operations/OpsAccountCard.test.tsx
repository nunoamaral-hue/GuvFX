import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { OpsAccountCard } from "@/components/operations/OpsAccountCard";
import type { BrokerAccount } from "@/types/broker";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    <a href={href} {...rest}>{children}</a>,
}));

const account = {
  id: 7, name: "Live A", broker_display_name: "Wayond", broker_name: "wayond",
  server_name: "Wayond-Demo", account_number: "12345678",
} as unknown as BrokerAccount;

describe("OpsAccountCard", () => {
  it("links to the account overview and masks the account number", () => {
    render(<OpsAccountCard account={account} />);
    const link = screen.getByRole("link", { name: /Operations overview for Live A/i });
    expect(link).toHaveAttribute("href", "/operations/accounts/7");
    // full number must never be rendered verbatim
    expect(screen.queryByText(/12345678/)).toBeNull();
  });
});
