import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import {
  formatCurrency,
  formatDate,
  formatNumber,
  getDictionaryEntries,
  localeFor,
} from "./i18n";

const SOURCE_ROOT = path.resolve(process.cwd(), "src");
const SOURCE_EXTENSIONS = new Set([".ts", ".tsx"]);

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const target = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(target);
    return SOURCE_EXTENSIONS.has(path.extname(entry.name)) ? [target] : [];
  });
}

function interpolationTokens(value: string): string[] {
  return [...value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)]
    .map((match) => match[1])
    .sort();
}

describe("English/Japanese translation contract", () => {
  it("provides non-empty English and Japanese for every key", () => {
    for (const [key, entry] of Object.entries(getDictionaryEntries())) {
      expect(entry.en.trim(), `${key}.en`).not.toBe("");
      expect(entry.ja.trim(), `${key}.ja`).not.toBe("");
    }
  });

  it("keeps interpolation variables identical between locales", () => {
    for (const [key, entry] of Object.entries(getDictionaryEntries())) {
      expect(interpolationTokens(entry.ja), key).toEqual(interpolationTokens(entry.en));
    }
  });

  it("defines every statically referenced translation key", () => {
    const keys = new Set(Object.keys(getDictionaryEntries()));
    const missing: string[] = [];
    const reference = /\bt\(\s*[^,]+,\s*["']([^"']+)["']\s*\)/g;
    for (const file of sourceFiles(SOURCE_ROOT)) {
      const relative = path.relative(SOURCE_ROOT, file);
      const source = fs.readFileSync(file, "utf8");
      for (const match of source.matchAll(reference)) {
        if (!keys.has(match[1])) missing.push(`${relative}: ${match[1]}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("keeps beta-critical Japanese terminology consistent", () => {
    const entries = getDictionaryEntries();
    expect(entries["nav.marketplace"].ja).toBe("マーケットプレイス");
    expect(entries["nav.myStrategies"].ja).toBe("利用中の戦略");
    expect(entries["myStrategies.title"].ja).toBe("利用中の戦略");
    expect(entries["hostedStatus.title"].ja).toBe("ホステッドワークスペース");
    expect(entries["configure.getStrategy"].ja).toBe("戦略を追加");
    expect(entries["enableModal.confirm"].ja).toBe("戦略を有効にする");
    expect(entries["configure.disable"].ja).toBe("戦略を停止する");
  });
});

describe("locale-sensitive formatting", () => {
  it("maps supported languages to explicit regional locales", () => {
    expect(localeFor("en")).toBe("en-GB");
    expect(localeFor("ja")).toBe("ja-JP");
  });

  it("formats dates, numbers, and currencies using the selected language", () => {
    const date = new Date("2026-08-18T12:00:00Z");
    expect(formatDate("en", date, { timeZone: "UTC" })).toMatch(/18.*Aug.*2026/);
    expect(formatDate("ja", date, { timeZone: "UTC" })).toContain("2026");
    expect(formatNumber("en", 1234.5)).toBe("1,234.5");
    expect(formatNumber("ja", 1234.5)).toBe("1,234.5");
    expect(formatCurrency("ja", 1234, "JPY")).toContain("1,234");
  });
});
