import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * Bounded hard-coded-copy guard for the closed-beta customer journey.
 *
 * This deliberately covers the high-risk stateful surfaces rather than every
 * frontend file. Product names and developer-only legacy recovery UI are not
 * made noisy repository-wide rules. When a new required beta surface is added,
 * add it here so its customer copy must enter the EN/JA catalogue from day one.
 */
const REQUIRED_SURFACES = [
  "src/lib/hosted-journey.ts",
  "src/components/hosted/HostedMt5RemoteApp.tsx",
  "src/components/accounts/HostedWorkspaceStatus.tsx",
  "src/components/onboarding/HostedWorkspaceJourney.tsx",
  "src/components/onboarding/OnboardingShell.tsx",
  "src/components/onboarding/steps/AccountConnectionStep.tsx",
  "src/components/onboarding/steps/BrokerStep.tsx",
  "src/components/onboarding/steps/EmailVerificationStep.tsx",
  "src/components/onboarding/steps/PlanSelectionStep.tsx",
  "src/components/onboarding/steps/ReadinessStep.tsx",
  "src/components/onboarding/steps/RiskAcceptanceStep.tsx",
  "src/components/onboarding/steps/StrategyAssignmentStep.tsx",
  "src/components/onboarding/steps/TwoFactorStep.tsx",
  "src/app/(public)/login/page.tsx",
  "src/app/(public)/register/page.tsx",
  "src/app/(app)/strategies/configure/page.tsx",
  "src/app/(app)/strategies/marketplace/page.tsx",
] as const;

const APPROVED_LITERAL_TEXT = new Set(["GuvFX", "?", "—"]);

function customerCopyViolations(source: string, fileName: string): string[] {
  const sourceFile = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const violations: string[] = [];

  const literalValue = (node: ts.Node): string | null =>
    ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) ? node.text : null;

  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      const value = node.text.replace(/&apos;/g, "'").replace(/\s+/g, " ").trim();
      if (/[A-Za-z]{2}/.test(value) && !APPROVED_LITERAL_TEXT.has(value)) violations.push(`JSX text: ${value}`);
    }

    if (ts.isJsxAttribute(node) && ["placeholder", "aria-label", "title"].includes(node.name.getText(sourceFile))) {
      const value = node.initializer && ts.isStringLiteral(node.initializer) ? node.initializer.text : null;
      if (value && /[A-Za-z]/.test(value)) violations.push(`customer attribute: ${value}`);
    }

    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
        /^set(?:Error|Info|Notice|TestMessage)$/.test(node.expression.text)) {
      const value = node.arguments[0] ? literalValue(node.arguments[0]) : null;
      if (value && /[A-Za-z]/.test(value)) violations.push(`customer state: ${value}`);
    }

    if (ts.isPropertyAssignment(node) && ts.isIdentifier(node.name) &&
        ["title", "body", "label", "description", "message"].includes(node.name.text)) {
      const value = literalValue(node.initializer);
      if (value && /[A-Za-z]/.test(value)) violations.push(`customer model: ${value}`);
    }

    ts.forEachChild(node, visit);
  };
  visit(sourceFile);

  return violations;
}

describe("required beta surfaces use the EN/JA catalogue", () => {
  for (const relativePath of REQUIRED_SURFACES) {
    it(relativePath, () => {
      const path = resolve(process.cwd(), relativePath);
      expect(customerCopyViolations(readFileSync(path, "utf8"), relativePath)).toEqual([]);
    });
  }

  it("hosted journey state models expose catalogue keys, not render-ready prose", () => {
    const path = resolve(process.cwd(), "src/lib/hosted-journey.ts");
    const source = readFileSync(path, "utf8");
    expect(source).not.toMatch(/\b(?:title|description|label)\s*:/);
    expect(source).toMatch(/\btitleKey\s*:/);
    expect(source).toMatch(/\bdescriptionKey\s*:/);
    expect(source).toMatch(/\blabelKey\s*:/);
  });
});
