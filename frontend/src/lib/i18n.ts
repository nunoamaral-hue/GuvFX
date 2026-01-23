/**
 * Simple i18n utility for GuvFX
 * Phase 1: EN/JP switching with cookie + localStorage persistence
 */

export type Lang = "en" | "ja";

const COOKIE_NAME = "guvfx_lang";
const STORAGE_KEY = "guvfx_lang";

// =============================================================================
// PERSISTENCE HELPERS
// =============================================================================

function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[$()*+./?[\\\]^{|}-]/g, "\\$&")}=([^;]*)`)
  );
  return match ? decodeURIComponent(match[1]) : null;
}

function setCookie(name: string, value: string, days: number = 365): void {
  if (typeof document === "undefined") return;
  const expires = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
}

function getLocalStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function setLocalStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Ignore storage errors
  }
}

// =============================================================================
// LANGUAGE DETECTION AND SETTING
// =============================================================================

/**
 * Detect the user's preferred language.
 * Priority: cookie > localStorage > navigator.language > "en"
 */
export function detectLang(): Lang {
  // 1. Check cookie
  const cookieLang = getCookie(COOKIE_NAME);
  if (cookieLang === "en" || cookieLang === "ja") {
    return cookieLang;
  }

  // 2. Check localStorage
  const storageLang = getLocalStorage(STORAGE_KEY);
  if (storageLang === "en" || storageLang === "ja") {
    return storageLang;
  }

  // 3. Check navigator.language
  if (typeof navigator !== "undefined") {
    const browserLang = navigator.language?.toLowerCase() || "";
    if (browserLang.startsWith("ja")) {
      return "ja";
    }
  }

  // 4. Default to English
  return "en";
}

/**
 * Set the user's language preference (persists to cookie + localStorage)
 */
export function setLang(lang: Lang): void {
  setCookie(COOKIE_NAME, lang);
  setLocalStorage(STORAGE_KEY, lang);
}

// =============================================================================
// DICTIONARY
// =============================================================================

type Dictionary = {
  [key: string]: {
    en: string;
    ja: string;
  };
};

const dictionary: Dictionary = {
  // -----------------------------------------------------------------------------
  // AppShell - Navigation Groups
  // -----------------------------------------------------------------------------
  "nav.strategy": { en: "Strategy", ja: "戦略" },
  "nav.run": { en: "Run", ja: "実行" },
  "nav.analytics": { en: "Analytics", ja: "分析" },
  "nav.settings": { en: "Settings", ja: "設定" },

  // -----------------------------------------------------------------------------
  // AppShell - Navigation Items
  // -----------------------------------------------------------------------------
  "nav.myStrategies": { en: "My Strategies", ja: "マイ戦略" },
  "nav.marketplace": { en: "Marketplace", ja: "マーケット" },
  "nav.createStrategy": { en: "Create Strategy", ja: "戦略作成" },
  "nav.strategyAdvisor": { en: "Strategy Advisor", ja: "戦略アドバイザー" },
  "nav.backtests": { en: "Backtests", ja: "バックテスト" },
  "nav.liveTrading": { en: "Live Trading", ja: "ライブ取引" },
  "nav.tradeHistory": { en: "Trade History", ja: "取引履歴" },
  "nav.overview": { en: "Overview", ja: "概要" },
  "nav.performance": { en: "Performance", ja: "パフォーマンス" },
  "nav.strategyMetrics": { en: "Strategy Metrics", ja: "戦略指標" },
  "nav.charts": { en: "Charts", ja: "チャート" },
  "nav.brokerAccounts": { en: "Broker Accounts", ja: "ブローカー口座" },
  "nav.userSettings": { en: "User Settings", ja: "ユーザー設定" },
  "nav.hosting": { en: "Hosting", ja: "ホスティング" },

  // -----------------------------------------------------------------------------
  // AppShell - UI Elements
  // -----------------------------------------------------------------------------
  "ui.tradingIntelligence": { en: "Trading Intelligence", ja: "トレーディングAI" },
  "ui.logout": { en: "Log out", ja: "ログアウト" },
  "ui.loggedIn": { en: "Logged in", ja: "ログイン中" },
  "ui.account": { en: "Account", ja: "アカウント" },
  "ui.profile": { en: "Profile", ja: "プロフィール" },
  "ui.settings": { en: "Settings", ja: "設定" },
  "ui.search": { en: "Search strategies, backtests...", ja: "戦略・バックテストを検索..." },
  "ui.soon": { en: "Soon", ja: "近日" },
  "ui.notifications": { en: "Notifications", ja: "通知" },

  // -----------------------------------------------------------------------------
  // Dashboard - Page
  // -----------------------------------------------------------------------------
  "dashboard.title": { en: "Dashboard", ja: "ダッシュボード" },
  "dashboard.subtitle": {
    en: "Unified trading intelligence across accounts and strategies.",
    ja: "口座と戦略を統合したトレーディングインテリジェンス",
  },

  // -----------------------------------------------------------------------------
  // Dashboard - Auth Banner
  // -----------------------------------------------------------------------------
  "dashboard.notLoggedIn": {
    en: "You are not logged in. Please sign in to access all features.",
    ja: "ログインしていません。すべての機能にアクセスするにはサインインしてください。",
  },
  "dashboard.logIn": { en: "Log in", ja: "ログイン" },
  "dashboard.signIn": { en: "Sign in →", ja: "サインイン →" },

  // -----------------------------------------------------------------------------
  // Dashboard - System Status Card
  // -----------------------------------------------------------------------------
  "dashboard.systemStatus": { en: "System Status", ja: "システム状況" },
  "dashboard.api": { en: "API", ja: "API" },
  "dashboard.session": { en: "Session", ja: "セッション" },
  "dashboard.checking": { en: "Checking...", ja: "確認中..." },
  "dashboard.online": { en: "Online", ja: "オンライン" },
  "dashboard.unavailable": { en: "Unavailable", ja: "利用不可" },
  "dashboard.authenticated": { en: "Authenticated", ja: "認証済み" },
  "dashboard.loginRequired": { en: "Login required", ja: "ログインが必要" },
  "dashboard.unknown": { en: "Unknown", ja: "不明" },

  // -----------------------------------------------------------------------------
  // Dashboard - Quick Actions Card
  // -----------------------------------------------------------------------------
  "dashboard.quickActions": { en: "Quick Actions", ja: "クイックアクション" },
  "dashboard.linkAccount": { en: "Link Account", ja: "口座を連携" },
  "dashboard.createStrategy": { en: "Create Strategy", ja: "戦略を作成" },
  "dashboard.exploreMarketplace": { en: "Explore Marketplace", ja: "マーケットを探索" },

  // -----------------------------------------------------------------------------
  // Dashboard - Signals Card
  // -----------------------------------------------------------------------------
  "dashboard.signals": { en: "Signals", ja: "シグナル" },
  "dashboard.accountsLinked": { en: "Accounts linked", ja: "連携口座数" },
  "dashboard.activeAccounts": { en: "Active accounts", ja: "アクティブ口座" },
  "dashboard.demoAccounts": { en: "Demo accounts", ja: "デモ口座" },

  // -----------------------------------------------------------------------------
  // Dashboard - Trading Accounts Card
  // -----------------------------------------------------------------------------
  "dashboard.tradingAccounts": { en: "Trading Accounts", ja: "取引口座" },
  "dashboard.loadingAccounts": { en: "Loading accounts...", ja: "口座を読み込み中..." },
  "dashboard.loginToViewAccounts": {
    en: "Login required to view accounts.",
    ja: "口座を表示するにはログインが必要です。",
  },
  "dashboard.unableToLoad": {
    en: "Unable to load accounts right now.",
    ja: "現在、口座を読み込めません。",
  },
  "dashboard.noAccountsLinked": { en: "No trading accounts linked", ja: "取引口座が連携されていません" },
  "dashboard.connectFirstAccount": {
    en: "Connect your first broker account to start tracking performance and deploying strategies.",
    ja: "最初のブローカー口座を連携して、パフォーマンスの追跡と戦略の展開を開始しましょう。",
  },
  "dashboard.accountsCount": { en: "account", ja: "口座" },
  "dashboard.accountsCountPlural": { en: "accounts", ja: "口座" },
  "dashboard.linked": { en: "linked", ja: "連携済み" },
  "dashboard.manage": { en: "Manage →", ja: "管理 →" },
  "dashboard.andMore": { en: "and", ja: "他" },
  "dashboard.more": { en: "more...", ja: "件..." },
  "dashboard.active": { en: "Active", ja: "アクティブ" },
  "dashboard.inactive": { en: "Inactive", ja: "非アクティブ" },
};

// =============================================================================
// TRANSLATION FUNCTION
// =============================================================================

/**
 * Get a translated string by key.
 * Falls back to English if key not found, or returns key if neither exists.
 */
export function t(lang: Lang, key: string): string {
  const entry = dictionary[key];
  if (!entry) {
    console.warn(`[i18n] Missing translation key: ${key}`);
    return key;
  }
  return entry[lang] || entry.en || key;
}

/**
 * Get all dictionary keys (useful for debugging/expansion)
 */
export function getDictionaryKeys(): string[] {
  return Object.keys(dictionary);
}
