/**
 * Simple i18n utility for GuvFX
 * Phase 1: EN/JP switching with cookie + localStorage persistence
 */

export type Lang = "en" | "ja";

export const localeFor = (lang: Lang): "en-GB" | "ja-JP" =>
  lang === "ja" ? "ja-JP" : "en-GB";

export function formatDate(
  lang: Lang,
  value: string | number | Date,
  options: Intl.DateTimeFormatOptions = {},
): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(localeFor(lang), {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...options,
  }).format(date);
}

export function formatNumber(
  lang: Lang,
  value: number,
  options?: Intl.NumberFormatOptions,
): string {
  return new Intl.NumberFormat(localeFor(lang), options).format(value);
}

export function formatCurrency(lang: Lang, value: number, currency: string): string {
  return formatNumber(lang, value, { style: "currency", currency });
}

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
  // Auth Gate — session verification / error states
  // -----------------------------------------------------------------------------
  "auth.sessionError": {
    en: "Session Unavailable",
    ja: "セッションが利用できません",
  },
  "auth.sessionErrorBody": {
    en: "We could not verify your session. This may be a temporary network issue. Please try logging in again.",
    ja: "セッションを確認できませんでした。一時的なネットワークの問題の可能性があります。再度ログインしてください。",
  },
  "auth.goToLogin": {
    en: "Go to Login",
    ja: "ログインへ",
  },
  "auth.verifyingSession": { en: "Verifying session…", ja: "セッションを確認しています…" },

  // -----------------------------------------------------------------------------
  // AppShell - Navigation Groups
  // -----------------------------------------------------------------------------
  "nav.getStarted": { en: "Get started", ja: "はじめに" },
  "nav.strategy": { en: "Strategy", ja: "戦略" },
  "nav.run": { en: "Run", ja: "実行" },
  "nav.analytics": { en: "Analytics", ja: "分析" },
  "nav.account": { en: "Account", ja: "アカウント" },
  "nav.settings": { en: "Settings", ja: "設定" },

  // -----------------------------------------------------------------------------
  // AppShell - Navigation Items
  // -----------------------------------------------------------------------------
  "nav.myStrategies": { en: "My Strategies", ja: "利用中の戦略" },
  "nav.marketplace": { en: "Marketplace", ja: "マーケットプレイス" },
  "nav.createStrategy": { en: "Create Strategy", ja: "戦略作成" },
  "nav.strategyAdvisor": { en: "Strategy Advisor", ja: "戦略アドバイザー" },
  "nav.backtests": { en: "Backtests", ja: "バックテスト" },
  "nav.liveTrading": { en: "Live Trading", ja: "ライブ取引" },
  "nav.terminalAccess": { en: "Terminal Access", ja: "ターミナルアクセス" },
  "nav.tradeHistory": { en: "Trade History", ja: "取引履歴" },
  "nav.overview": { en: "Overview", ja: "概要" },
  "nav.performance": { en: "Performance", ja: "パフォーマンス" },
  "nav.strategyMetrics": { en: "Strategy Metrics", ja: "戦略指標" },
  "nav.strategyLab": { en: "Strategy Lab", ja: "戦略ラボ" },
  "nav.charts": { en: "Charts", ja: "チャート" },
  "nav.billingPlans": { en: "Billing & Plans", ja: "請求 & プラン" },
  "nav.accountHosting": { en: "Hosting", ja: "ホスティング" },
  "nav.invoices": { en: "Invoices", ja: "請求書" },
  "nav.usage": { en: "Usage", ja: "使用状況" },
  "nav.brokerAccounts": { en: "Broker Accounts", ja: "ブローカー口座" },
  "nav.brokerConnections": { en: "Broker Connections", ja: "ブローカー接続" },
  "nav.userSettings": { en: "User Settings", ja: "ユーザー設定" },
  "nav.hosting": { en: "Hosting", ja: "ホスティング" },

  // Operations Console
  "nav.operations": { en: "Operations", ja: "運用管理" },
  "nav.opsOverview": { en: "Overview", ja: "概要" },
  "nav.operationsSupport": { en: "Operations & Support", ja: "運用・サポート" },
  "nav.reconciliation": { en: "Reconciliation", ja: "照合" },
  "nav.payments": { en: "Payments", ja: "決済" },
  "nav.workers": { en: "Workers", ja: "ワーカー" },
  "nav.entitlements": { en: "Entitlements", ja: "権限" },
  "nav.executionJobs": { en: "Execution Jobs", ja: "実行ジョブ" },

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
  "ui.home": { en: "Home", ja: "ホーム" },
  "ui.languageLabel": { en: "Language", ja: "言語" },
  "ui.english": { en: "English", ja: "英語" },
  "ui.japanese": { en: "Japanese", ja: "日本語" },
  "ui.langEnglish": { en: "English", ja: "English" },
  "ui.langJapanese": { en: "日本語", ja: "日本語" },

  // Customer Telegram notifications
  "telegram.title": { en: "Telegram notifications", ja: "Telegram通知" },
  "telegram.subtitle": {
    en: "Receive trading updates from GuvFX directly in Telegram.",
    ja: "GuvFXの取引更新をTelegramで直接受け取れます。",
  },
  "telegram.connected": { en: "Connected", ja: "接続済み" },
  "telegram.connecting": { en: "Connecting…", ja: "接続中…" },
  "telegram.notConnected": { en: "Not connected", ja: "未接続" },
  "telegram.connectedAs": { en: "Connected as", ja: "接続先:" },
  "telegram.connect": { en: "Connect Telegram", ja: "Telegramを接続" },
  "telegram.disconnect": { en: "Disconnect", ja: "接続を解除" },
  "telegram.waiting": { en: "Waiting for Telegram…", ja: "Telegramでの操作を待っています…" },
  "telegram.startPrompt": {
    en: "Telegram has opened in a new window. Press Start there to finish connecting.",
    ja: "新しいウィンドウでTelegramが開きました。Telegramで「開始」を押すと接続が完了します。",
  },
  "telegram.unavailable": {
    en: "Telegram connection is not available yet.",
    ja: "Telegram接続は現在ご利用いただけません。",
  },
  "telegram.error": {
    en: "We couldn’t update Telegram notifications. Please try again.",
    ja: "Telegram通知を更新できませんでした。もう一度お試しください。",
  },
  "telegram.preferences": { en: "Notification preferences", ja: "通知設定" },
  "telegram.preferencesDetail": {
    en: "Choose which completed results and account updates you receive.",
    ja: "受け取る取引結果や口座情報を選択できます。",
  },
  "telegram.pref.winners": { en: "Winning trades", ja: "利益になった取引" },
  "telegram.pref.winnersDetail": { en: "Completed winning results. On by default.", ja: "利益が確定した取引結果。初期設定はオンです。" },
  "telegram.pref.losers": { en: "Losing trades", ja: "損失になった取引" },
  "telegram.pref.losersDetail": { en: "Completed losing or breakeven results. Off by default.", ja: "損失または損益なしで終了した取引結果。初期設定はオフです。" },
  "telegram.pref.tpProgress": { en: "Take-profit progress", ja: "テイクプロフィットの進捗" },
  "telegram.pref.tpProgressDetail": { en: "Signal-safe progress from durable completed positions.", ja: "決済済みポジションに基づく安全な進捗通知。" },
  "telegram.pref.system": { en: "System/account messages", ja: "システム・口座のお知らせ" },
  "telegram.pref.systemDetail": { en: "Optional workspace, connection and action-required updates.", ja: "ワークスペース、接続、対応が必要な場合のお知らせ。" },
  "telegram.master": { en: "Telegram notifications", ja: "Telegram通知" },
  "telegram.masterDetail": { en: "Turn all Telegram updates on or off.", ja: "すべてのTelegram通知をオンまたはオフにします。" },
  "telegram.pref.tradeUpdated": { en: "Trade updates", ja: "取引の進捗" },
  "telegram.pref.tradeUpdatedDetail": { en: "When durable take-profit or trade progress is recorded.", ja: "テイクプロフィットまたは取引の進捗が確定したとき。" },
  "telegram.pref.tradeClosed": { en: "Trade closed", ja: "取引終了" },
  "telegram.pref.tradeClosedDetail": { en: "When a trade is closed with its final result.", ja: "取引が終了し、最終損益が確定したとき。" },
  "telegram.pref.strategy": { en: "Strategy enabled or disabled", ja: "ストラテジーの有効化・無効化" },
  "telegram.pref.strategyDetail": { en: "When automated trading is started or paused.", ja: "自動売買を開始または一時停止したとき。" },
  "telegram.pref.problem": { en: "Trading needs attention", ja: "取引に確認が必要" },
  "telegram.pref.problemDetail": { en: "Customer-safe updates when trading needs attention.", ja: "取引に確認が必要な場合の、お客様向けの安全な通知。" },
  "telegram.pref.workspace": { en: "Workspace ready", ja: "ワークスペース準備完了" },
  "telegram.pref.workspaceDetail": { en: "When your hosted trading workspace is ready.", ja: "ホスト型取引ワークスペースの準備が完了したとき。" },
  "common.close": { en: "Close", ja: "閉じる" },
  "common.done": { en: "Done", ja: "完了" },

  // -----------------------------------------------------------------------------
  // Dashboard - Page
  // -----------------------------------------------------------------------------
  "dashboard.title": { en: "Dashboard", ja: "ダッシュボード" },
  "dashboard.subtitle": {
    en: "Overview of your strategies, tests, and trading accounts.",
    ja: "戦略、テスト、取引口座の概要。",
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
    en: "Set up your trading workspace to start tracking performance and deploying strategies.",
    ja: "取引ワークスペースをセットアップして、パフォーマンスの追跡と戦略の展開を開始しましょう。",
  },
  "dashboard.accountsCount": { en: "account", ja: "口座" },
  "dashboard.accountsCountPlural": { en: "accounts", ja: "口座" },
  "dashboard.linked": { en: "linked", ja: "連携済み" },
  "dashboard.manage": { en: "Manage →", ja: "管理 →" },
  "dashboard.andMore": { en: "and", ja: "他" },
  "dashboard.more": { en: "more...", ja: "件..." },
  "dashboard.active": { en: "Active", ja: "アクティブ" },
  "dashboard.inactive": { en: "Inactive", ja: "非アクティブ" },
  "dashboard.trustMiniBody": {
    en: "You control all trading decisions. This platform does not execute trades without your action.",
    ja: "すべての取引判断はあなたが行います。操作なしに取引が実行されることはありません。",
  },

  // -----------------------------------------------------------------------------
  // Accounts - Page Header
  // -----------------------------------------------------------------------------
  "accounts.title": { en: "Broker Accounts", ja: "ブローカー口座" },
  "accounts.subtitle": {
    en: "Link your broker / MT5 accounts so GuvFX can map strategies and trades.",
    ja: "ブローカー/MT5口座を連携し、GuvFXで戦略と取引を紐付けます。",
  },
  // Hosted Workspace customers manage a GuvFX-run MetaTrader workspace, not a manually-linked broker (P2).
  "accounts.hostedTitle": { en: "Trading Workspace", ja: "取引ワークスペース" },
  "accounts.hostedSubtitle": {
    en: "Your managed MetaTrader workspace and its status. GuvFX runs it for you — you log in inside MetaTrader.",
    ja: "GuvFXが運用する MetaTrader ワークスペースとその状態です。ログインは MetaTrader 内で行います。",
  },

  // -----------------------------------------------------------------------------
  // Accounts - Add Account Card
  // -----------------------------------------------------------------------------
  "accounts.addTitle": { en: "Add Trading Account", ja: "取引口座を追加" },
  "accounts.addSubtitle": {
    en: "Create a link to a broker or MT5 account. GuvFX will use this for mapping strategies and trades.",
    ja: "ブローカーまたはMT5口座へのリンクを作成します。GuvFXは戦略と取引の紐付けに使用します。",
  },

  // -----------------------------------------------------------------------------
  // Accounts - Form Labels
  // -----------------------------------------------------------------------------
  "accounts.accountName": { en: "Account name", ja: "口座名" },
  "accounts.accountNameHelp": {
    en: "This is a friendly name for you to recognise the account on your list.",
    ja: "リストで口座を識別するための表示名です。",
  },
  "accounts.accountNamePlaceholder": { en: "e.g. Main MT5", ja: "例: メインMT5" },
  "accounts.brokerServerName": { en: "Broker server name", ja: "ブローカーサーバー名" },
  "accounts.brokerServerNameHelp": {
    en: "This is the server name of your broker! If you are unsure, check directly with your broker what this is. It is usually in the email you receive from your broker with your access details.",
    ja: "ブローカーのサーバー名です。不明な場合はブローカーに直接確認してください。通常、アクセス情報と一緒にブローカーから届くメールに記載されています。",
  },
  "accounts.brokerServerPlaceholder": { en: "e.g. Broker-Live01 or Broker-Demo02", ja: "例: Broker-Live01 または Broker-Demo02" },
  "accounts.accountNumber": { en: "Account number / login", ja: "口座番号 / ログインID" },
  "accounts.accountNumberHelp": {
    en: "This is the account number used to login via your broker's MetaTrader account.",
    ja: "ブローカーのMetaTrader口座にログインするための口座番号です。",
  },
  "accounts.accountNumberPlaceholder": { en: "e.g. 123456", ja: "例: 123456" },
  "accounts.platformPassword": { en: "Platform password", ja: "プラットフォームパスワード" },
  "accounts.platformPasswordHelp": {
    en: "This is your broker platform password (e.g. MetaTrader 5) for the traditional connection. It is encrypted and used only to connect the trading account you already have. Prefer not to share it? Use a hosted workspace instead — you log in inside MetaTrader and GuvFX never receives it.",
    ja: "従来型接続で使うブローカーのプラットフォーム（例: MetaTrader 5）のパスワードです。暗号化され、お持ちの取引口座への接続にのみ使用されます。共有したくない場合は、ホスト型ワークスペースをご利用ください（MetaTrader 内でログインし、GuvFX がパスワードを受け取ることはありません）。",
  },
  "accounts.platformPasswordPlaceholder": {
    en: "Password used in MetaTrader / broker platform",
    ja: "MetaTrader/ブローカープラットフォームのパスワード",
  },
  "accounts.accountType": { en: "Account type", ja: "口座タイプ" },
  "accounts.demoAccount": { en: "Demo account", ja: "デモ口座" },

  // -----------------------------------------------------------------------------
  // Accounts - Broker Suggestions
  // -----------------------------------------------------------------------------
  "accounts.searchingBrokers": { en: "Searching broker servers…", ja: "ブローカーサーバーを検索中…" },
  "accounts.noBrokersFound": { en: "No matching broker servers found.", ja: "一致するブローカーサーバーが見つかりません。" },
  "accounts.selected": { en: "Selected:", ja: "選択中:" },

  // -----------------------------------------------------------------------------
  // Accounts - Buttons
  // -----------------------------------------------------------------------------
  "accounts.addAccount": { en: "Add account", ja: "口座を追加" },
  "accounts.creating": { en: "Creating…", ja: "作成中…" },
  "accounts.testConnection": { en: "Test MT5 connection", ja: "MT5接続テスト" },
  "accounts.testing": { en: "Testing…", ja: "テスト中…" },
  "accounts.activeClickDeactivate": { en: "Active (click to deactivate)", ja: "有効（クリックで無効化）" },
  "accounts.inactiveClickActivate": { en: "Inactive (click to activate)", ja: "無効（クリックで有効化）" },

  // -----------------------------------------------------------------------------
  // Accounts - Linked Accounts Card
  // -----------------------------------------------------------------------------
  "accounts.linkedTitle": { en: "Linked Accounts", ja: "連携済み口座" },
  "accounts.loadingAssignments": { en: "Loading strategy assignments…", ja: "戦略割り当てを読み込み中…" },
  "accounts.loadingAccounts": { en: "Loading accounts…", ja: "口座を読み込み中…" },
  "accounts.noLinkedAccounts": {
    en: "No trading accounts linked yet. Use the form above to add one.",
    ja: "連携済みの取引口座がありません。上のフォームから追加してください。",
  },
  "accounts.accountNumberLabel": { en: "Account number:", ja: "口座番号:" },
  "accounts.brokerServerLabel": { en: "Broker server:", ja: "ブローカーサーバー:" },
  "accounts.createdLabel": { en: "Created:", ja: "作成日:" },

  // -----------------------------------------------------------------------------
  // Accounts - Messages
  // -----------------------------------------------------------------------------
  "accounts.failedToLoad": { en: "Failed to load trading accounts", ja: "取引口座の読み込みに失敗しました" },
  "accounts.accountAdded": { en: "Account connected successfully.", ja: "口座を接続しました。" },
  "accounts.testSuccess": { en: "Connection verified.", ja: "接続を確認しました。" },
  "accounts.testFailed": { en: "Connection not verified:", ja: "接続を確認できませんでした:" },
  "accounts.setActive": { en: "Account set to ACTIVE.", ja: "口座を有効に設定しました。" },
  "accounts.setInactive": { en: "Account set to INACTIVE.", ja: "口座を無効に設定しました。" },
  "accounts.failedActiveStatus": { en: "Failed to change active status", ja: "有効/無効の切り替えに失敗しました" },
  "accounts.loadingWorkspace": { en: "Loading your workspace…", ja: "ワークスペースを読み込んでいます…" },
  "accounts.workspaceUnavailable": { en: "We couldn't load your workspace status right now.", ja: "現在、ワークスペースの状況を読み込めません。" },
  "accounts.verifyLoginError": { en: "We couldn't verify your broker login. Check your details and try again.", ja: "ブローカーへのログインを確認できませんでした。入力内容を確認し、もう一度お試しください。" },
  "accounts.invalidLogin": { en: "We couldn't add the account because the login details aren't valid.", ja: "ログイン情報が正しくないため、口座を追加できませんでした。" },
  "accounts.setup.connectTitle": { en: "Connect your broker account", ja: "取引口座を接続" },
  "accounts.setup.connectBody": { en: "GuvFX requires an MT5 broker account. Your credentials are encrypted, and setup and validation happen automatically after you submit them below. Use a demo account during Trusted Beta.", ja: "GuvFXの利用にはMT5取引口座が必要です。認証情報は暗号化され、以下から送信すると設定と確認が自動的に行われます。トラステッドベータではデモ口座をご利用ください。" },
  "accounts.setup.readyTitle": { en: "Your account is ready", ja: "口座の準備ができました" },
  "accounts.setup.readyBody": { en: "Your broker connection is set up. Choose a strategy to continue.", ja: "ブローカーへの接続が完了しました。続けるには戦略を選択してください。" },
  "accounts.setup.chooseStrategy": { en: "Choose a strategy", ja: "戦略を選択" },
  "accounts.setup.failedTitle": { en: "We couldn't complete setup", ja: "設定を完了できませんでした" },
  "accounts.setup.failedBody": { en: "Re-enter your broker details below to try again.", ja: "もう一度試すには、以下にブローカー情報を再入力してください。" },
  "accounts.setup.completeTitle": { en: "Complete your broker connection", ja: "取引口座への接続を完了" },
  "accounts.setup.completeBody": { en: "Re-enter your broker details below to start setting up your dedicated terminal.", ja: "専用ターミナルの設定を始めるには、以下にブローカー情報を再入力してください。" },
  "accounts.setup.connectingTitle": { en: "Connecting your broker account", ja: "取引口座に接続しています" },

  // -----------------------------------------------------------------------------
  // Login - Page Header
  // -----------------------------------------------------------------------------
  "login.welcomeBack": { en: "Welcome back to", ja: "おかえりなさい" },
  "login.subtitle": {
    en: "Log in to manage strategies, review backtests, and get AI-powered guidance on your trading.",
    ja: "ログインして戦略の管理、バックテストの確認、AIによる取引ガイダンスを利用しましょう。",
  },
  "login.logIn": { en: "Log in", ja: "ログイン" },
  "login.home": { en: "Home", ja: "ホーム" },
  "login.goToSignUp": { en: "Go to Sign up", ja: "新規登録へ" },

  // -----------------------------------------------------------------------------
  // Login - Form Panel
  // -----------------------------------------------------------------------------
  "login.panelTitle": { en: "Log in", ja: "ログイン" },
  "login.panelSubtitle": { en: "Welcome back — enter your GuvFX credentials.", ja: "おかえりなさい — GuvFXの認証情報を入力してください。" },
  "login.email": { en: "Email", ja: "メールアドレス" },
  "login.emailPlaceholder": { en: "Email", ja: "メールアドレス" },
  "login.password": { en: "Password", ja: "パスワード" },
  "login.passwordPlaceholder": { en: "Your password", ja: "パスワード" },
  "login.continue": { en: "Continue", ja: "続行" },
  "login.loggingIn": { en: "Logging in...", ja: "ログイン中..." },

  // -----------------------------------------------------------------------------
  // Login - Reason Messages
  // -----------------------------------------------------------------------------
  "login.reasonExpired": { en: "Your token has expired, please login again.", ja: "トークンの有効期限が切れました。再度ログインしてください。" },
  "login.reasonUnauthenticated": { en: "Please log in to continue.", ja: "続行するにはログインしてください。" },
  "login.reasonLoggedOut": { en: "You have been logged out.", ja: "ログアウトしました。" },

  // -----------------------------------------------------------------------------
  // Login - Validation & Success Messages
  // -----------------------------------------------------------------------------
  "login.errorEmptyFields": { en: "Please enter your email and password.", ja: "メールアドレスとパスワードを入力してください。" },
  "login.errorDefault": { en: "Login failed. Please check your credentials.", ja: "ログインに失敗しました。認証情報を確認してください。" },
  "login.success": { en: "Logged in successfully. Redirecting…", ja: "ログイン成功。リダイレクト中…" },

  // -----------------------------------------------------------------------------
  // Landing Page - Navbar
  // -----------------------------------------------------------------------------
  "landing.logoAlt": { en: "GuvFX Logo", ja: "GuvFXロゴ" },
  "landing.login": { en: "Log in", ja: "ログイン" },
  "landing.navLogin": { en: "Trader Login", ja: "トレーダーログイン" },
  "landing.getStarted": { en: "Get Started", ja: "始める" },

  // -----------------------------------------------------------------------------
  // Landing Page - Hero Section
  // -----------------------------------------------------------------------------
  "landing.heroTitle": { en: "Automated Trading Intelligence", ja: "自動トレーディングインテリジェンス" },
  "landing.heroSubtitle": {
    en: "Design algorithmic strategies, run backtests, and deploy with AI-powered analysis. Built for serious traders.",
    ja: "アルゴリズム戦略の設計、バックテストの実行、AIによる分析を活用した展開。本格的なトレーダーのために構築。",
  },
  "landing.heroProof": {
    en: "Built for discretionary and systematic traders. No hype. Full control.",
    ja: "裁量・システムトレーダー向け。誇張なし。完全なコントロール。",
  },
  "landing.ctaPrimary": { en: "Start Free Trial", ja: "無料トライアルを開始" },
  "landing.ctaSecondary": { en: "View Platform", ja: "プラットフォームを見る" },
  "landing.ctaMicro": {
    en: "No credit card • Cancel anytime • Demo supported",
    ja: "クレジットカード不要・いつでも解約・デモ対応",
  },
  "landing.heroCTA": { en: "Start Building", ja: "戦略を始める" },
  "landing.heroSecondaryCTA": { en: "Learn More", ja: "詳細を見る" },

  // -----------------------------------------------------------------------------
  // Landing Page - Capability Section (What You Can Do)
  // -----------------------------------------------------------------------------
  "landing.capTitle": { en: "What you can do with GuvFX", ja: "GuvFXでできること" },
  "landing.cap1Title": { en: "Design Strategies", ja: "戦略を設計" },
  "landing.cap1Body": {
    en: "Build rule-based and discretionary systems with full control.",
    ja: "完全なコントロールでルール型・裁量型戦略を構築。",
  },
  "landing.cap2Title": { en: "Test Before Risk", ja: "リスク前に検証" },
  "landing.cap2Body": {
    en: "Backtest and forward-test before touching live capital.",
    ja: "実運用前にバックテスト・フォワードテスト。",
  },
  "landing.cap3Title": { en: "Deploy with Confidence", ja: "安全に実行" },
  "landing.cap3Body": {
    en: "Connect MT5 accounts and manage execution safely.",
    ja: "MT5口座と接続し、安全に運用管理。",
  },

  // -----------------------------------------------------------------------------
  // Landing Page - Trust Section
  // -----------------------------------------------------------------------------
  "landing.trustTitle": { en: "Built for execution discipline", ja: "実行規律のための設計" },
  "landing.trustBody": {
    en: "Designed with capital protection, auditability, and execution discipline in mind.",
    ja: "資本保護、監査性、実行規律を重視して設計。",
  },
  "landing.trustB1": { en: "No black-box strategies", ja: "ブラックボックス戦略なし" },
  "landing.trustB2": { en: "Deterministic execution", ja: "決定的な実行" },
  "landing.trustB3": { en: "Manual + automated workflows", ja: "裁量 + 自動の両立" },
  "landing.trustB4": { en: "Full account separation", ja: "口座の完全分離" },

  // -----------------------------------------------------------------------------
  // Landing Page - Trust & Clarity Section (Legal-first)
  // -----------------------------------------------------------------------------
  "landing.trustHeadline": {
    en: "Trust & Clarity",
    ja: "信頼と透明性",
  },
  "landing.trustSub": {
    en: "GuvFX is a technology platform for strategy management. We do not provide investment advice.",
    ja: "GuvFXは戦略管理のための技術プラットフォームです。投資助言は行いません。",
  },
  "landing.trustPoint1Title": { en: "Full Transparency", ja: "完全な透明性" },
  "landing.trustPoint1Body": {
    en: "Every rule and parameter is visible. No hidden logic or black-box decisions.",
    ja: "すべてのルールとパラメータが確認可能。隠されたロジックやブラックボックスはありません。",
  },
  "landing.trustPoint2Title": { en: "You Stay in Control", ja: "あなたが主導権を持つ" },
  "landing.trustPoint2Body": {
    en: "Nothing runs without your explicit action. Review, approve, and execute on your terms.",
    ja: "明示的な操作なしに実行されることはありません。確認・承認・実行はすべてあなた次第。",
  },
  "landing.trustPoint3Title": { en: "Test Before Execution", ja: "実行前にテスト" },
  "landing.trustPoint3Body": {
    en: "Backtest strategies against historical data. Understand behavior before any live execution.",
    ja: "過去データで戦略をバックテスト。ライブ実行前に動作を把握。",
  },
  "landing.trustPoint4Title": { en: "Account Separation", ja: "口座の分離" },
  "landing.trustPoint4Body": {
    en: "Each broker account is isolated. Strategies operate only where you assign them.",
    ja: "各ブローカー口座は独立。戦略は指定した場所でのみ動作します。",
  },
  "landing.blackBoxHeadline": { en: "Not a black box", ja: "ブラックボックスではない" },
  "landing.blackBoxBody": {
    en: "Every strategy parameter is explicit and editable. You see exactly what the system will do.",
    ja: "すべての戦略パラメータは明示的で編集可能。システムの動作を正確に確認できます。",
  },
  "landing.controlHeadline": { en: "You control execution", ja: "実行はあなたが管理" },
  "landing.controlBody": {
    en: "No trades are placed without your approval. Automated execution requires explicit setup and confirmation.",
    ja: "承認なしに取引が行われることはありません。自動実行には明示的な設定と確認が必要です。",
  },
  "landing.learnCTA": { en: "How it works", ja: "仕組みを見る" },
  "landing.viewDemoCTA": { en: "Explore dashboard", ja: "ダッシュボードを見る" },
  "landing.disclaimerInline": {
    en: "Platform tools only — not financial advice.",
    ja: "ツール提供のみ — 投資助言ではありません。",
  },

  // -----------------------------------------------------------------------------
  // Landing Page - Language Suggestion Prompt
  // -----------------------------------------------------------------------------
  "landing.langPrompt": { en: "Prefer Japanese?", ja: "日本語で表示しますか？" },
  "landing.langYes": { en: "Switch to Japanese", ja: "日本語に切り替える" },
  "landing.langNo": { en: "Not now", ja: "後で" },

  // -----------------------------------------------------------------------------
  // Landing Page - Features Section
  // -----------------------------------------------------------------------------
  "landing.featuresTitle": { en: "Platform Features", ja: "プラットフォーム機能" },
  "landing.featuresSubtitle": {
    en: "Everything you need to develop, test, and run algorithmic trading strategies.",
    ja: "アルゴリズム取引戦略の開発、テスト、実行に必要なすべてが揃っています。",
  },

  "landing.feature1Title": { en: "Strategy Builder", ja: "戦略ビルダー" },
  "landing.feature1Desc": {
    en: "Visual and code-based tools to create trading strategies without complexity.",
    ja: "複雑さなしに取引戦略を作成するためのビジュアルおよびコードベースのツール。",
  },

  "landing.feature2Title": { en: "Backtesting Engine", ja: "バックテストエンジン" },
  "landing.feature2Desc": {
    en: "Test strategies against historical data with detailed performance metrics.",
    ja: "詳細なパフォーマンス指標で過去のデータに対して戦略をテスト。",
  },

  "landing.feature3Title": { en: "AI Strategy Advisor", ja: "AI戦略アドバイザー" },
  "landing.feature3Desc": {
    en: "Get AI-powered insights and recommendations to refine your approach.",
    ja: "AIによる洞察と推奨事項でアプローチを改善。",
  },

  "landing.feature4Title": { en: "Multi-Broker Support", ja: "マルチブローカー対応" },
  "landing.feature4Desc": {
    en: "Connect to MT5 and major brokers. Manage multiple accounts in one place.",
    ja: "MT5と主要ブローカーに接続。複数の口座を一元管理。",
  },

  // -----------------------------------------------------------------------------
  // Landing Page - Footer
  // -----------------------------------------------------------------------------
  "landing.footerTagline": { en: "Trading Intelligence Platform", ja: "トレーディングインテリジェンスプラットフォーム" },
  "landing.footerCopyright": { en: "© 2025 GuvFX. All rights reserved.", ja: "© 2025 GuvFX. All rights reserved." },
  "landing.footerDisclaimer": {
    en: "Trading involves risk. Past performance does not guarantee future results.",
    ja: "取引にはリスクが伴います。過去の実績は将来の結果を保証するものではありません。",
  },

  // -----------------------------------------------------------------------------
  // How It Works Page
  // -----------------------------------------------------------------------------
  "howItWorks.title": {
    en: "How GuvFX Works",
    ja: "GuvFXの仕組み",
  },
  "howItWorks.subtitle": {
    en: "GuvFX is a technology platform for building, testing, and managing trading strategies. It is not a broker, not an investment advisor, and does not provide financial advice.",
    ja: "GuvFXは取引戦略の構築・テスト・管理のための技術プラットフォームです。ブローカーでも投資顧問でもなく、金融助言は行いません。",
  },
  "howItWorks.sectionWhatIsTitle": {
    en: "What GuvFX provides",
    ja: "GuvFXが提供するもの",
  },
  "howItWorks.sectionWhatIsBody": {
    en: "GuvFX gives you tools to define, test, and manage rule-based trading strategies. You configure every parameter, review every result, and decide when and how to execute.",
    ja: "GuvFXはルールベースの取引戦略を定義・テスト・管理するツールを提供します。すべてのパラメータを設定し、結果を確認し、実行の判断はあなたが行います。",
  },
  "howItWorks.toolDesign": {
    en: "Strategy design tools — define rules, indicators, and risk parameters",
    ja: "戦略設計ツール — ルール、指標、リスクパラメータの定義",
  },
  "howItWorks.toolTest": {
    en: "Backtesting engine — test strategies against historical data",
    ja: "バックテストエンジン — 過去データで戦略をテスト",
  },
  "howItWorks.toolExecute": {
    en: "Execution controls — user-configured, user-approved deployment",
    ja: "実行制御 — ユーザーが設定し、ユーザーが承認するデプロイ",
  },
  "howItWorks.sectionWhatNotTitle": {
    en: "What GuvFX is not",
    ja: "GuvFXではないもの",
  },
  "howItWorks.bullet1": {
    en: "Not a black-box bot — every rule is visible and editable",
    ja: "ブラックボックスではない — すべてのルールが確認・編集可能",
  },
  "howItWorks.bullet2": {
    en: "Not a signal service — no trade recommendations are provided",
    ja: "シグナルサービスではない — 取引推奨は行いません",
  },
  "howItWorks.bullet3": {
    en: "Not financial advice — platform tools only",
    ja: "金融助言ではない — ツール提供のみ",
  },
  "howItWorks.bullet4": {
    en: "Not a guarantee of outcomes — past results do not predict future performance",
    ja: "結果の保証ではない — 過去の結果は将来のパフォーマンスを予測しません",
  },
  "howItWorks.sectionControlTitle": {
    en: "Control & Transparency",
    ja: "制御と透明性",
  },
  "howItWorks.sectionControlBody": {
    en: "GuvFX is designed so that you remain in full control at every step. Nothing happens without your explicit action.",
    ja: "GuvFXはすべてのステップであなたが完全に制御できるよう設計されています。明示的な操作なしには何も実行されません。",
  },
  "howItWorks.control1": {
    en: "Nothing runs by default — all execution requires explicit setup",
    ja: "デフォルトでは何も実行されない — すべての実行に明示的な設定が必要",
  },
  "howItWorks.control2": {
    en: "User enables execution — you choose what runs and where",
    ja: "ユーザーが実行を有効化 — 何をどこで実行するか選択",
  },
  "howItWorks.control3": {
    en: "User can stop or disable at any time",
    ja: "いつでも停止・無効化が可能",
  },
  "howItWorks.control4": {
    en: "All strategy rules are visible and auditable",
    ja: "すべての戦略ルールが確認・監査可能",
  },
  "howItWorks.sectionWorkflowTitle": {
    en: "Safe Workflow",
    ja: "安全なワークフロー",
  },
  "howItWorks.workflowStep1": {
    en: "Define — Build your strategy with explicit rules and parameters",
    ja: "定義 — 明確なルールとパラメータで戦略を構築",
  },
  "howItWorks.workflowStep2": {
    en: "Test — Run backtests against historical data to observe behavior",
    ja: "テスト — 過去データでバックテストし動作を観察",
  },
  "howItWorks.workflowStep3": {
    en: "Review — Examine results and understand risk characteristics",
    ja: "確認 — 結果を精査しリスク特性を理解",
  },
  "howItWorks.workflowStep4": {
    en: "Decide — You choose whether to proceed with live execution",
    ja: "判断 — ライブ実行に進むかどうかはあなたが決定",
  },
  "howItWorks.nextTitle": {
    en: "Get started",
    ja: "始める",
  },
  "howItWorks.ctaDashboard": {
    en: "Explore dashboard",
    ja: "ダッシュボードを見る",
  },
  "howItWorks.ctaCreateStrategy": {
    en: "Create a strategy",
    ja: "戦略を作成する",
  },
  "howItWorks.ctaLinkAccount": {
    en: "Link a trading account",
    ja: "取引口座を連携する",
  },

  // -----------------------------------------------------------------------------
  // Register Page
  // -----------------------------------------------------------------------------
  "register.welcomeTo": { en: "Welcome to", ja: "ようこそ" },
  "register.subtitle": {
    en: "Manage strategies, backtests, broker connectivity, and execution workflows from one governed platform.",
    ja: "戦略、バックテスト、ブローカー接続、実行ワークフローを一つの統合プラットフォームで管理。",
  },
  "register.getStarted": { en: "Get started", ja: "始める" },
  "register.login": { en: "Log in", ja: "ログイン" },
  "register.signUp": { en: "Sign up", ja: "新規登録" },
  "register.createAccount": { en: "Create your account", ja: "アカウントを作成" },
  "register.stepIndicator": { en: "Step 1 of 5", ja: "ステップ 1/5" },
  "register.stepTitle": { en: "Create Account", ja: "アカウント作成" },
  "register.stepNote": {
    en: "Create your account to begin the GuvFX setup process.",
    ja: "GuvFXのセットアッププロセスを開始するためにアカウントを作成してください。",
  },
  "register.nextTitle": { en: "Next steps", ja: "次のステップ" },
  "register.nextPlan": { en: "Select plan", ja: "プランを選択" },
  "register.nextProfile": { en: "Complete profile", ja: "プロフィールを完成" },
  "register.nextBroker": { en: "Open workspace", ja: "ワークスペースを開く" },
  "register.nextReview": { en: "Review setup", ja: "セットアップを確認" },
  "register.email": { en: "Email", ja: "メールアドレス" },
  "register.emailPlaceholder": { en: "Email", ja: "メールアドレス" },
  "register.password": { en: "Password", ja: "パスワード" },
  "register.passwordPlaceholder": { en: "Minimum 8 characters", ja: "8文字以上" },
  "register.username": { en: "Username (optional)", ja: "ユーザー名（任意）" },
  "register.usernamePlaceholder": { en: "Leave blank to use email", ja: "空欄でメールを使用" },
  "register.continue": { en: "Continue", ja: "続行" },
  "register.creating": { en: "Creating account...", ja: "アカウント作成中..." },
  "register.passwordTooShort": { en: "Password must be at least 8 characters.", ja: "パスワードは8文字以上必要です。" },
  "register.success": { en: "Account created for {email}. You can now log in.", ja: "{email}のアカウントが作成されました。ログインできます。" },
  "register.errorDefault": { en: "Registration failed.", ja: "登録に失敗しました。" },
  "register.trustMiniTitle": { en: "Built for controlled trading operations", ja: "管理された取引運用のために構築" },
  "register.trustMiniBody": {
    en: "GuvFX provides a guided workspace for strategies, broker connectivity, backtesting, and execution management.",
    ja: "GuvFXは戦略、ブローカー接続、バックテスト、実行管理のためのガイド付きワークスペースを提供します。",
  },

  // -----------------------------------------------------------------------------
  // Register Page — Step 2: Email Verification (RESERVED)
  // -----------------------------------------------------------------------------
  "register.step2Title": { en: "Verify Your Email", ja: "メールアドレスの確認" },
  "register.step2Subtitle": { en: "Check your inbox for a verification link.", ja: "受信トレイの確認リンクをクリックしてください。" },
  "register.step2Note": {
    en: "Email verification is required to link live trading accounts.",
    ja: "ライブ取引口座を連携するにはメール認証が必要です。",
  },
  "register.verifyEmail": { en: "Verify email", ja: "メールを確認" },
  "register.verificationSent": { en: "Verification email sent to {email}.", ja: "{email}に確認メールを送信しました。" },
  "register.resendVerification": { en: "Resend verification email", ja: "確認メールを再送信" },

  // -----------------------------------------------------------------------------
  // Register Page — Step 3: Hosting Selection (RESERVED)
  // -----------------------------------------------------------------------------
  "register.step3Title": { en: "Hosting Selection", ja: "ホスティング選択" },
  "register.step3Subtitle": { en: "Choose where your strategies will execute.", ja: "戦略を実行する場所を選択してください。" },
  "register.step3Note": {
    en: "Hosting is required to deploy strategies. You can change this later.",
    ja: "戦略をデプロイするにはホスティングが必要です。後で変更できます。",
  },
  "register.selectRegion": { en: "Select region", ja: "リージョンを選択" },
  "register.selectTier": { en: "Select tier", ja: "ティアを選択" },
  "register.hostingTerms": {
    en: "I acknowledge that hosting resources are subject to the hosting terms of service.",
    ja: "ホスティングリソースはホスティング利用規約に従うことを了承します。",
  },

  // -----------------------------------------------------------------------------
  // Register Page — Step 4: Profile & Compliance (RESERVED)
  // -----------------------------------------------------------------------------
  "register.step4Title": { en: "Profile & Compliance", ja: "プロフィール・コンプライアンス" },
  "register.step4Subtitle": { en: "Complete your profile and acknowledge platform terms.", ja: "プロフィールを完成させ、プラットフォーム規約を確認してください。" },
  "register.step4Note": {
    en: "Required for full platform access. All information is kept confidential.",
    ja: "全機能へのアクセスに必要です。すべての情報は機密として扱われます。",
  },
  "register.riskDisclosure": {
    en: "I understand that trading in financial instruments carries risk and past performance does not guarantee future results.",
    ja: "金融商品の取引にはリスクが伴い、過去の実績は将来の結果を保証しないことを理解しています。",
  },
  "register.platformTerms": {
    en: "I accept the platform terms of service.",
    ja: "プラットフォーム利用規約に同意します。",
  },
  "register.notAdviceAck": {
    en: "I understand that GuvFX provides strategy management tools only and does not provide investment advice. I am solely responsible for all trading decisions.",
    ja: "GuvFXは戦略管理ツールのみを提供し、投資助言は行わないことを理解しています。すべての取引判断は自己責任です。",
  },

  // -----------------------------------------------------------------------------
  // Register Page — Step 5: Security Setup (RESERVED)
  // -----------------------------------------------------------------------------
  "register.step5Title": { en: "Security Setup", ja: "セキュリティ設定" },
  "register.step5Subtitle": { en: "Protect your account with additional security.", ja: "追加のセキュリティでアカウントを保護してください。" },
  "register.step5Note": {
    en: "Two-factor authentication is optional but recommended for account security.",
    ja: "二要素認証は任意ですが、アカウントのセキュリティのために推奨されます。",
  },
  "register.setup2FA": { en: "Set up two-factor authentication", ja: "二要素認証を設定" },
  "register.skipForNow": { en: "Skip for now", ja: "今はスキップ" },
  "register.generateRecoveryCodes": { en: "Generate recovery codes", ja: "リカバリーコードを生成" },

  // -----------------------------------------------------------------------------
  // Register Page — Completion (RESERVED)
  // -----------------------------------------------------------------------------
  "register.registrationComplete": {
    en: "Registration complete. Welcome to GuvFX.",
    ja: "登録完了。GuvFXへようこそ。",
  },
  "register.resumeRegistration": {
    en: "Resume registration",
    ja: "登録を再開",
  },
  "register.incompleteRegistration": {
    en: "Complete your registration to unlock all features.",
    ja: "すべての機能をアンロックするには登録を完了してください。",
  },

  // -----------------------------------------------------------------------------
  // Legal Footer Disclaimer
  // -----------------------------------------------------------------------------
  "legal.footerLine1": {
    en: "Trading in financial instruments carries risk. Past performance does not guarantee future results.",
    ja: "金融商品の取引にはリスクが伴います。過去の実績は将来の結果を保証するものではありません。",
  },
  "legal.footerLine2": {
    en: "GuvFX is a strategy management platform and does not provide investment advice.",
    ja: "GuvFXは戦略管理プラットフォームであり、投資助言を提供するものではありません。",
  },
  "legal.microDisclaimer": {
    en: "Platform tools only — not financial advice.",
    ja: "本プラットフォームはツール提供のみで、投資助言ではありません。",
  },

  // -----------------------------------------------------------------------------
  // Onboarding Checklist (Dashboard)
  // -----------------------------------------------------------------------------
  "onboarding.title": {
    en: "Getting started with GuvFX",
    ja: "GuvFXの始め方",
  },
  "onboarding.step1": {
    en: "Link a trading account",
    ja: "取引口座を連携する",
  },
  "onboarding.step2": {
    en: "Create your first strategy",
    ja: "最初の戦略を作成する",
  },
  "onboarding.step3": {
    en: "Run a backtest",
    ja: "バックテストを実行する",
  },
  "onboarding.step4": {
    en: "Review results before execution",
    ja: "実行前に結果を確認する",
  },
  "onboarding.footerNote": {
    en: "You control all decisions. GuvFX does not place trades on your behalf.",
    ja: "すべての判断はユーザーが行います。GuvFXが取引を実行することはありません。",
  },
  "onboarding.dismiss": {
    en: "Got it, don't show again",
    ja: "了解、次回から表示しない",
  },

  // -----------------------------------------------------------------------------
  // Strategy Marketplace
  // -----------------------------------------------------------------------------
  "marketplace.title": {
    en: "Strategy Marketplace",
    ja: "戦略マーケットプレイス",
  },
  "marketplace.subtitle": {
    en: "Browse and deploy strategy templates to your trading accounts.",
    ja: "戦略テンプレートを閲覧し、取引口座に展開できます。",
  },
  "marketplace.disclaimerLine1": {
    en: "Templates and examples only. No financial advice. Any figures shown are illustrative and not a guarantee of outcomes.",
    ja: "テンプレート・例示のみ。投資助言ではありません。表示される数値は例示であり、結果を保証するものではありません。",
  },
  "marketplace.styleLabel": {
    en: "Style",
    ja: "スタイル",
  },
  "marketplace.timeframesLabel": {
    en: "Timeframes",
    ja: "時間足",
  },
  "marketplace.executionLabel": {
    en: "Execution",
    ja: "実行",
  },
  "marketplace.pairsLabel": {
    en: "Pairs",
    ja: "通貨ペア",
  },
  "marketplace.searchPlaceholder": {
    en: "Search templates, pairs\u2026",
    ja: "テンプレート・通貨ペアを検索\u2026",
  },
  "marketplace.filterAll": {
    en: "All",
    ja: "すべて",
  },
  "marketplace.filterTrend": {
    en: "Trend",
    ja: "トレンド",
  },
  "marketplace.filterBreakout": {
    en: "Breakout",
    ja: "ブレイクアウト",
  },
  "marketplace.filterReversion": {
    en: "Reversion",
    ja: "リバージョン",
  },
  "marketplace.filterPatterns": {
    en: "Patterns",
    ja: "パターン",
  },
  "marketplace.filterSystem-grade": {
    en: "System-grade",
    ja: "システムグレード",
  },
  "marketplace.tag.template": { en: "Template", ja: "テンプレート" },
  "marketplace.tag.example": { en: "Example", ja: "例" },
  "marketplace.tag.beta": { en: "Beta", ja: "ベータ" },
  "marketplace.tag.automation-ready": { en: "Automation-ready", ja: "自動化対応" },
  "marketplace.tag.ali": { en: "Ali", ja: "Ali" },
  "marketplace.tag.alts": { en: "ALTS", ja: "ALTS" },
  "marketplace.tag.sce": { en: "SCE", ja: "SCE" },
  "marketplace.tag.hybrid": { en: "Hybrid", ja: "ハイブリッド" },
  "marketplace.tag.signal-copy": { en: "Signal copy", ja: "シグナルコピー" },
  "marketplace.tag.demo": { en: "Demo", ja: "デモ" },
  "marketplace.strategy.mp-001.summary": { en: "Example ruleset for Asian session range breakouts during London open. Review and test before use.", ja: "ロンドン市場開始時のアジア時間帯レンジ・ブレイクアウト用ルール例です。利用前に内容を確認し、テストしてください。" },
  "marketplace.strategy.mp-001.style": { en: "Volatility Breakout", ja: "ボラティリティ・ブレイクアウト" },
  "marketplace.strategy.mp-001.execution": { en: "Manual review required", ja: "手動確認が必要" },
  "marketplace.strategy.mp-002.summary": { en: "20/50 EMA cross on M15 with H4 trend alignment. Designed to be configured and tested by the user.", ja: "H4のトレンド方向に合わせたM15の20/50 EMAクロスです。お客様が設定し、テストして使用します。" },
  "marketplace.strategy.mp-002.style": { en: "Trend Following", ja: "トレンドフォロー" },
  "marketplace.strategy.mp-002.execution": { en: "Manual review required", ja: "手動確認が必要" },
  "marketplace.strategy.mp-003.summary": { en: "Enters on 2σ touches with RSI divergence. Example template — review and test before use.", ja: "2σへの到達とRSIダイバージェンスを条件とするテンプレート例です。利用前に内容を確認し、テストしてください。" },
  "marketplace.strategy.mp-003.style": { en: "Mean Reversion", ja: "平均回帰" },
  "marketplace.strategy.mp-003.execution": { en: "Manual review required", ja: "手動確認が必要" },
  "marketplace.strategy.mp-004.summary": { en: "Automated chart pattern recognition for H&S reversals with volume confirmation. Currently in beta — review and test before use.", ja: "出来高確認を伴うヘッド・アンド・ショルダー反転パターンを検出します。現在ベータ版のため、利用前に内容を確認し、テストしてください。" },
  "marketplace.strategy.mp-004.style": { en: "Chart Patterns", ja: "チャートパターン" },
  "marketplace.strategy.mp-004.execution": { en: "User-controlled execution", ja: "お客様が実行を管理" },
  "marketplace.strategy.mp-005.summary": { en: "HTF zone + trendline break + structure shift. Fixed 2R model. Manual zones editable. Designed by Ali.", ja: "上位足ゾーン、トレンドライン・ブレイク、構造転換を組み合わせた固定2Rモデルです。手動ゾーンを編集できます。Ali設計。" },
  "marketplace.strategy.mp-005.style": { en: "HTF Zone + Structure", ja: "上位足ゾーン＋構造" },
  "marketplace.strategy.mp-005.execution": { en: "Automation-ready", ja: "自動化対応" },
  "marketplace.strategy.mp-006.summary": { en: "Range-regime liquidity sweep + displacement + confirmation. M5 execution with M15 regime filter.", ja: "レンジ局面の流動性スイープ、変位、確認を組み合わせ、M15の局面フィルターでM5を使用します。" },
  "marketplace.strategy.mp-006.style": { en: "Liquidity / Mean reversion", ja: "流動性／平均回帰" },
  "marketplace.strategy.mp-006.execution": { en: "Automation-ready", ja: "自動化対応" },
  "marketplace.strategy.mp-007.summary": { en: "H4 bias + H1 BOS + pullback + rejection continuation. H1 execution with H4 context.", ja: "H4の方向、H1の構造転換、押し戻り、反発継続を組み合わせ、H4の文脈でH1を使用します。" },
  "marketplace.strategy.mp-007.style": { en: "Trend continuation", ja: "トレンド継続" },
  "marketplace.strategy.mp-007.execution": { en: "Automation-ready", ja: "自動化対応" },
  "marketplace.strategy.mp-009.summary": { en: "CORE (TBP trendline break pocket) + SLEEVE (TC1 trend continuation on risk-on days). H4 execution.", ja: "CORE（TBPトレンドライン・ブレイク）とSLEEVE（リスクオン日のTC1トレンド継続）を組み合わせ、H4を使用します。" },
  "marketplace.strategy.mp-009.style": { en: "HTF Zone + Macro Overlay", ja: "上位足ゾーン＋マクロオーバーレイ" },
  "marketplace.strategy.mp-009.execution": { en: "Automation-ready", ja: "自動化対応" },
  "marketplace.strategy.mp-010.summary": { en: "When enabled, automatically mirrors a curated Telegram signal provider into your demo account.", ja: "有効にすると、選定されたTelegramシグナル提供元の取引をデモ口座へ自動的に反映します。" },
  "marketplace.strategy.mp-010.style": { en: "Telegram signal copy", ja: "Telegramシグナルコピー" },
  "marketplace.strategy.mp-010.execution": { en: "Automated signal copy · demo", ja: "自動シグナルコピー・デモ" },
  "marketplace.selectAccount": {
    en: "Select account",
    ja: "口座を選択",
  },
  "marketplace.assign": {
    en: "Assign",
    ja: "割当",
  },
  // WS-G — generic-card blocked states: say exactly what's missing + the next action.
  "marketplace.assignNeedsAccount": {
    en: "You'll need a broker account first. Add one to assign this strategy.",
    ja: "まずブローカー口座が必要です。この戦略を割り当てるには口座を追加してください。",
  },
  "marketplace.assignSelectAccountHint": {
    en: "Choose the account to assign this strategy to.",
    ja: "この戦略を割り当てる口座を選択してください。",
  },
  "marketplace.assigning": {
    en: "Assigning\u2026",
    ja: "割当中\u2026",
  },
  // AJ#7 — "Get Strategy" acquisition journey (Get -> Configure -> Enable).
  "marketplace.getStrategy": {
    en: "Get Strategy",
    ja: "戦略を追加",
  },
  "marketplace.getting": {
    en: "Adding…",
    ja: "追加中…",
  },
  "marketplace.getSelectAccountHint": {
    en: "Choose the account to use this strategy on.",
    ja: "この戦略を利用する口座を選択してください。",
  },
  "marketplace.getNeedsSignIn": {
    en: "Sign in to get this strategy.",
    ja: "この戦略を追加するにはサインインしてください。",
  },
  "marketplace.getNeedsAccount": {
    en: "You'll need a broker account first. Add one to get this strategy.",
    ja: "まずブローカー口座が必要です。この戦略を追加するには口座を追加してください。",
  },
  "marketplace.getFailed": {
    en: "We couldn't add this strategy just now. Please try again.",
    ja: "現在この戦略を追加できませんでした。もう一度お試しください。",
  },
  "marketplace.priceLabel": {
    en: "Price",
    ja: "価格",
  },
  "marketplace.configure": {
    en: "Configure",
    ja: "設定",
  },
  "marketplace.manage": {
    en: "Manage",
    ja: "管理",
  },
  "marketplace.stateOwned": {
    en: "Setup required",
    ja: "設定が必要",
  },
  "marketplace.stateEnabled": {
    en: "Enabled",
    ja: "有効",
  },
  "marketplace.stateNeedsAttention": {
    en: "Needs attention",
    ja: "要確認",
  },
  "marketplace.copyStatusLabel": {
    en: "Status",
    ja: "状態",
  },
  "marketplace.copyOn": {
    en: "Enabled",
    ja: "有効",
  },
  "marketplace.copyOff": {
    en: "Disabled",
    ja: "無効",
  },
  "marketplace.copyNotArmedShort": {
    en: "Not set up",
    ja: "未設定",
  },
  "marketplace.copyEnable": {
    en: "Enable",
    ja: "有効化",
  },
  "marketplace.copyDisable": {
    en: "Disable",
    ja: "無効化",
  },
  "marketplace.copyWorking": {
    en: "Working…",
    ja: "処理中…",
  },
  "marketplace.copyEnabled": {
    en: "Strategy enabled.",
    ja: "戦略を有効化しました。",
  },
  "marketplace.copyDisabled": {
    en: "Strategy disabled.",
    ja: "戦略を無効化しました。",
  },
  "marketplace.copyNotArmed": {
    en: "This strategy isn't set up for your account yet.",
    ja: "この戦略はまだお客様の口座に設定されていません。",
  },
  "marketplace.copyAmbiguous": {
    en: "This copy strategy needs attention. Please contact support.",
    ja: "このコピー戦略は確認が必要です。サポートにお問い合わせください。",
  },
  "marketplace.copyNotArmedHint": {
    en: "Set up your account to enable automatic copying.",
    ja: "自動コピーを有効にするには口座を設定してください。",
  },
  "marketplace.copyToggleFailed": {
    en: "Could not change the strategy state. Please try again.",
    ja: "戦略の状態を変更できませんでした。もう一度お試しください。",
  },
  // IPR Area D — self-service Enable-Trading (arm). Customer-safe wording keyed off the arm
  // response's machine-readable status (never the raw detail/slug).
  "marketplace.armEnableTrading": {
    en: "Enable Trading",
    ja: "取引を有効化",
  },
  "marketplace.armWorking": {
    en: "Enabling…",
    ja: "有効化しています…",
  },
  // AJ#6.5 — the Wayond card OWNS the forward path for a hosted-ready customer (Option B). The concepts stay
  // distinct on the card: MT5 capability → customer authorization ("Enable automated trading", ADR-0047) →
  // strategy arm ("Enable this strategy"). These NEVER bounce the customer back to onboarding.
  "marketplace.enableAutomatedTrading": {
    en: "Enable automated trading",
    ja: "自動売買を有効化",
  },
  "marketplace.armEnableStrategy": {
    en: "Enable this strategy",
    ja: "この戦略を有効化",
  },
  "marketplace.authorizeHint": {
    en: "Your workspace is ready. Enable automated trading to let GuvFX run the strategies you turn on.",
    ja: "ワークスペースの準備が整いました。自動売買を有効にすると、有効化した戦略をGuvFXが実行します。",
  },
  // Onboarding-complete but the workspace is not yet ready to trade (e.g. AutoTrading not switched on, or the
  // market is closed). The card OWNS this state (no bounce back to onboarding) and reassures the customer.
  "marketplace.hostedPreparingHint": {
    en: "Your workspace is finishing getting ready for automated trading. This usually completes shortly.",
    ja: "ワークスペースは自動売買の準備を仕上げています。まもなく完了します。",
  },
  "marketplace.authorizeSuccess": {
    en: "Automated trading enabled. You can now turn on this strategy.",
    ja: "自動売買を有効にしました。この戦略を有効化できます。",
  },
  "marketplace.authorizeFailed": {
    en: "We couldn't enable automated trading just now. Please try again in a moment.",
    ja: "現在、自動売買を有効にできませんでした。しばらくしてから再度お試しください。",
  },
  "marketplace.armSuccess": {
    en: "Trading enabled for this account.",
    ja: "このアカウントで取引を有効化しました。",
  },
  "marketplace.armSelectAccountHint": {
    en: "Choose the demo account to copy signals into.",
    ja: "シグナルをコピーするデモアカウントを選択してください。",
  },
  "marketplace.copyAmbiguousShort": {
    en: "Needs attention",
    ja: "要確認",
  },
  "marketplace.armDisabled": {
    en: "Self-service trading isn't available yet. Please contact support.",
    ja: "セルフサービスの取引はまだ利用できません。サポートにお問い合わせください。",
  },
  "marketplace.armAccountNotReady": {
    en: "This account must be a demo account and active before you can enable trading.",
    ja: "取引を有効化する前に、このアカウントはデモかつ有効である必要があります。",
  },
  "marketplace.armCredentialsMissing": {
    en: "Add and validate your MT5 login for this account first.",
    ja: "先にこのアカウントのMT5ログインを追加して検証してください。",
  },
  "marketplace.armRuntimeNotReady": {
    en: "Your account is still getting ready to trade. Try again shortly.",
    ja: "口座はまだ取引の準備中です。しばらくしてから再度お試しください。",
  },
  "marketplace.armBrokerNotConnected": {
    en: "We're still connecting to your broker. Try again shortly.",
    ja: "ブローカーへの接続を行っています。しばらくしてから再度お試しください。",
  },
  "marketplace.armSingleTenant": {
    en: "Another account is already enabled for this signal. Only one can run at a time.",
    ja: "別のアカウントがこのシグナルで既に有効化されています。同時に実行できるのは1つだけです。",
  },
  "marketplace.armAccountNotFound": {
    en: "That account wasn't found. Refresh and try again.",
    ja: "そのアカウントが見つかりませんでした。更新して再度お試しください。",
  },
  "marketplace.armFailed": {
    en: "Couldn't enable trading. Please try again.",
    ja: "取引を有効化できませんでした。もう一度お試しください。",
  },
  // WS-G — the arm rejections that were previously collapsed into the generic "try again" toast. These
  // are permanent / attention states, so they must NOT read as retriable.
  "marketplace.armNotPilotApproved": {
    en: "Automatic trading isn't available for your account yet. Please contact support to request access.",
    ja: "自動取引はまだご利用いただけません。ご希望の場合はサポートにお問い合わせください。",
  },
  "marketplace.armValidationUnhealthy": {
    en: "We couldn't verify your broker connection. Please re-check your login and try again.",
    ja: "ブローカー接続を確認できませんでした。ログイン情報を確認して再度お試しください。",
  },
  "marketplace.armPaused": {
    en: "Trading is paused while we check your connection. It will resume automatically.",
    ja: "接続を確認する間、取引は一時停止しています。自動的に再開されます。",
  },
  "marketplace.armDuplicate": {
    en: "This account already has trading enabled for another signal.",
    ja: "このアカウントは既に別のシグナルで取引が有効になっています。",
  },
  // WS-D — the readiness panel that replaces the opaque "Not armed" hint: a ✓/✕ checklist + one clear
  // next action, all customer-safe (the backend returns only machine keys/codes; the copy lives here).
  "marketplace.readinessTitle": {
    en: "What's needed",
    ja: "必要な手順",
  },
  "marketplace.readinessLoading": {
    en: "Checking your account…",
    ja: "口座を確認しています…",
  },
  "marketplace.readinessUnavailable": {
    en: "We couldn't check your account status right now.",
    ja: "現在、口座の状態を確認できませんでした。",
  },
  "marketplace.readinessNoDemo": {
    en: "You'll need a demo account first. Add one on your Accounts page.",
    ja: "まずデモ口座が必要です。アカウントページで追加してください。",
  },
  "marketplace.readinessAddAccount": {
    en: "Go to Broker Accounts",
    ja: "ブローカー口座へ移動",
  },
  "marketplace.readinessCheckDemo": {
    en: "Demo account",
    ja: "デモ口座",
  },
  "marketplace.readinessCheckActive": {
    en: "Account active",
    ja: "口座が有効",
  },
  "marketplace.readinessCheckCredentials": {
    en: "Broker login added",
    ja: "ブローカーのログインを追加済み",
  },
  "marketplace.readinessCheckRuntime": {
    en: "Account ready to trade",
    ja: "取引の準備完了",
  },
  "marketplace.readinessCheckAccess": {
    en: "Trading access enabled",
    ja: "取引アクセスが有効",
  },
  "marketplace.readinessNextClosed": {
    en: "This account is disconnected. Its history is preserved.",
    ja: "この口座は切断されています。履歴は保持されます。",
  },
  "marketplace.readinessNextAddDemo": {
    en: "This strategy runs on a demo account. Add a demo account to continue.",
    ja: "この戦略はデモ口座で動作します。続行するにはデモ口座を追加してください。",
  },
  "marketplace.readinessNextActivate": {
    en: "This account isn't active. Activate it on your Accounts page to continue.",
    ja: "この口座は有効ではありません。アカウントページで有効化してください。",
  },
  "marketplace.readinessNextTradingOn": {
    en: "Trading is on for this account.",
    ja: "この口座で取引が有効です。",
  },
  "marketplace.readinessNextResume": {
    en: "Trading is set up but paused. Enable it to resume copying.",
    ja: "取引は設定済みですが一時停止中です。有効化するとコピーを再開します。",
  },
  "marketplace.readinessNextReady": {
    en: "Your account is ready. Enable trading to start copying.",
    ja: "口座の準備ができました。取引を有効化してコピーを開始してください。",
  },
  "marketplace.navGoAccounts": {
    en: "Go to Accounts",
    ja: "口座へ移動",
  },
  "marketplace.navActivateAccount": {
    en: "Continue setup",
    ja: "セットアップを続ける",
  },
  "marketplace.navFinishWorkspace": {
    en: "Continue setup",
    ja: "セットアップを続ける",
  },
  "marketplace.navOpenWorkspace": {
    en: "Continue",
    ja: "続ける",
  },
  "marketplace.navViewStrategies": {
    en: "View my strategies",
    ja: "マイ戦略を見る",
  },
  "marketplace.readinessRetry": {
    en: "Try again",
    ja: "再試行",
  },
  "marketplace.assignNeedsSignIn": {
    en: "Sign in to assign this strategy to your account.",
    ja: "この戦略を口座に割り当てるにはサインインしてください。",
  },
  "marketplace.unauthMessage": {
    en: "You are not authenticated. Please log in again to assign marketplace templates.",
    ja: "認証されていません。マーケットプレイスのテンプレートを割り当てるには再ログインしてください。",
  },
  "marketplace.goToLogin": {
    en: "Go to Login \u2192",
    ja: "ログインへ \u2192",
  },
  "marketplace.viewMyStrategies": {
    en: "View in My Strategies \u2192",
    ja: "マイ戦略で確認 \u2192",
  },
  "marketplace.alertSelectAccount": {
    en: "Please select an account first.",
    ja: "先に口座を選択してください。",
  },
  "marketplace.alertAssigned": {
    en: "Assigned successfully.",
    ja: "割り当てが完了しました。",
  },
  "marketplace.alertSessionExpired": {
    en: "Your session has expired. Please log in again.",
    ja: "セッションが切れました。再ログインしてください。",
  },
  "marketplace.alertEndpointNotFound": {
    en: "We couldn't add this template right now. Please try again shortly.",
    ja: "現在このテンプレートを追加できませんでした。しばらくしてから再度お試しください。",
  },
  "marketplace.alertUnexpectedResponse": {
    en: "We couldn't add this template right now. Please refresh and try again.",
    ja: "現在このテンプレートを追加できませんでした。ページを更新して再度お試しください。",
  },
  "marketplace.alertAssignFailed": {
    en: "We couldn't add this template. Please try again.",
    ja: "このテンプレートを追加できませんでした。もう一度お試しください。",
  },
  "marketplace.alertPlanRestricted": {
    en: "Your plan doesn't include assigning strategies. Upgrade your plan to continue.",
    ja: "現在のプランには戦略の割り当てが含まれていません。続行するにはプランをアップグレードしてください。",
  },
  "marketplace.emptyTitle": {
    en: "No templates match your filters.",
    ja: "フィルターに一致するテンプレートがありません。",
  },
  "marketplace.emptyHint": {
    en: "Try adjusting your search or category filter.",
    ja: "検索条件やカテゴリーフィルターを変更してみてください。",
  },
  "marketplace.featured": {
    en: "Featured",
    ja: "注目",
  },
  "marketplace.moreComingSoon": {
    en: "More strategies coming soon.",
    ja: "さらに戦略を追加予定です。",
  },

  // -----------------------------------------------------------------------------
  // Create Strategy
  // -----------------------------------------------------------------------------
  "createStrategy.title": {
    en: "Create Strategy",
    ja: "戦略を作成",
  },
  "createStrategy.subtitle": {
    en: "Build a strategy template from idea to structure. You can refine details later on the strategy page.",
    ja: "アイデアから構造まで戦略テンプレートを構築します。詳細は戦略ページで後から調整できます。",
  },
  "createStrategy.showAdvanced": {
    en: "Show advanced",
    ja: "詳細設定を表示",
  },
  "createStrategy.hideAdvanced": {
    en: "Hide advanced",
    ja: "詳細設定を非表示",
  },
  "createStrategy.advancedHint": {
    en: "Advanced = indicators, filters, psychology, and extra risk controls.",
    ja: "詳細設定 = インジケーター、フィルター、心理管理、追加リスク管理。",
  },
  "createStrategy.overviewTitle": {
    en: "0) Overview",
    ja: "0) 概要",
  },
  "createStrategy.overviewSubtitle": {
    en: "Give your strategy a name and optional description.",
    ja: "戦略に名前と任意の説明を付けてください。",
  },
  "createStrategy.strategyNameLabel": {
    en: "Strategy name",
    ja: "戦略名",
  },
  "createStrategy.descriptionLabel": {
    en: "Description (optional)",
    ja: "説明（任意）",
  },
  "createStrategy.archetypeTitle": {
    en: "1) Strategy archetype",
    ja: "1) 戦略アーキタイプ",
  },
  "createStrategy.archetypeSubtitle": {
    en: "Pick a template. Defaults auto-fill below.",
    ja: "テンプレートを選択してください。デフォルト値が下に自動入力されます。",
  },
  "createStrategy.suggested": {
    en: "Suggested",
    ja: "おすすめ",
  },
  "createStrategy.hypothesisLabel": {
    en: "Hypothesis (optional)",
    ja: "仮説（任意）",
  },
  "createStrategy.hypothesisHelp": {
    en: "Describe the hypothesis and what conditions it depends on. No performance claims.",
    ja: "仮説と前提条件を記載してください（成果の断定は不可）。",
  },
  "createStrategy.backtestingNote": {
    en: "After saving, run tests to observe behavior and review risk characteristics before enabling execution.",
    ja: "保存後にテストを実行し、実行を有効にする前に挙動とリスク特性を確認してください。",
  },
  "createStrategy.tradeLogicTitle": {
    en: "4) Trade logic",
    ja: "4) トレードロジック",
  },
  "createStrategy.approachTypeLabel": {
    en: "Approach type",
    ja: "アプローチタイプ",
  },
  "createStrategy.selectApproach": {
    en: "Select approach type",
    ja: "アプローチタイプを選択",
  },
  "createStrategy.rationaleLabel": {
    en: "Rationale",
    ja: "根拠",
  },
  "createStrategy.aiAssistLabel": {
    en: "Let AI assist with parameter defaults",
    ja: "AIにパラメーター設定を補助させる",
  },
  "createStrategy.riskControlsTitle": {
    en: "10) Risk controls",
    ja: "10) リスク管理",
  },
  "createStrategy.stopExitTitle": {
    en: "4) Stop & exit rules",
    ja: "4) ストップ＆終了ルール",
  },
  "createStrategy.exitRulesLabel": {
    en: "Exit rules",
    ja: "終了ルール",
  },
  "createStrategy.exitRulesTitle": {
    en: "6) Exit rules",
    ja: "6) 終了ルール",
  },

  // -----------------------------------------------------------------------------
  // Backtests
  // -----------------------------------------------------------------------------
  "backtests.title": {
    en: "Backtests",
    ja: "バックテスト",
  },
  "backtests.subtitle": {
    en: "Manage test configurations, launch runs, and review observed results.",
    ja: "テスト設定の管理、実行の起動、結果の確認を行います。",
  },
  "backtests.disclaimerLine1": {
    en: "Testing is informational only. Results depend on data quality and assumptions, and do not guarantee future outcomes.",
    ja: "テストは情報提供のみを目的としています。結果はデータの品質と仮定に依存し、将来の結果を保証するものではありません。",
  },
  "backtests.detailTitle": {
    en: "Test Configuration",
    ja: "テスト設定",
  },
  "backtests.detailSubtitle": {
    en: "Review the configuration and all runs associated with it.",
    ja: "設定と関連するすべての実行を確認します。",
  },
  "backtests.detailEmptyHint": {
    en: "Click 'Run test' to create a demo run, then 'Process pending runs' to generate illustrative results.",
    ja: "「テスト実行」をクリックしてデモ実行を作成し、「保留中の実行を処理」で例示的な結果を生成します。",
  },
  "backtests.observedReturn": {
    en: "Observed return",
    ja: "観測リターン",
  },
  "backtests.maxDrawdown": {
    en: "Max drawdown",
    ja: "最大ドローダウン",
  },
  "backtests.observedWinRate": {
    en: "Observed hit rate",
    ja: "観測ヒット率",
  },
  "backtests.observedHitRateAvg": {
    en: "Observed hit rate",
    ja: "観測ヒット率",
  },
  "backtests.avgAcrossRuns": {
    en: "Average across runs",
    ja: "実行全体の平均",
  },
  "backtests.emptyTitle": {
    en: "No test configurations yet",
    ja: "テスト設定がまだありません",
  },
  "backtests.emptySubtitle": {
    en: "Create a strategy first, then return here to set up test configurations.",
    ja: "まず戦略を作成し、テスト設定を行うためにここに戻ってください。",
  },
  "backtests.ctaCreateStrategy": {
    en: "Create a strategy",
    ja: "戦略を作成",
  },
  "backtests.ctaLinkAccount": {
    en: "Link a trading account",
    ja: "取引口座を連携",
  },
  "backtests.configsCardTitle": {
    en: "Test Configurations",
    ja: "テスト設定",
  },
  "backtests.lastProcessed": {
    en: "Last processed:",
    ja: "最終処理:",
  },
  "backtests.pendingNotProcessed": {
    en: "Pending runs have not been processed yet in this session.",
    ja: "保留中の実行はまだ処理されていません。",
  },
  "backtests.processPending": {
    en: "Process pending runs",
    ja: "保留中を処理",
  },
  "backtests.processing": {
    en: "Processing…",
    ja: "処理中…",
  },
  "backtests.loading": {
    en: "Loading test configurations…",
    ja: "テスト設定を読み込み中…",
  },
  "backtests.noRuns": {
    en: "No runs",
    ja: "実行なし",
  },
  "backtests.strategyLabel": {
    en: "Strategy:",
    ja: "戦略:",
  },
  "backtests.noDescription": {
    en: "No description",
    ja: "説明なし",
  },
  "backtests.symbolLabel": {
    en: "Symbol:",
    ja: "シンボル:",
  },
  "backtests.timeframeLabel": {
    en: "Timeframe:",
    ja: "時間足:",
  },
  "backtests.periodLabel": {
    en: "Period:",
    ja: "期間:",
  },
  "backtests.initialBalanceLabel": {
    en: "Initial balance:",
    ja: "初期残高:",
  },
  "backtests.runsLabel": {
    en: "Runs:",
    ja: "実行回数:",
  },
  "backtests.lastRunLabel": {
    en: "Last run:",
    ja: "最終実行:",
  },
  "backtests.noEquityData": {
    en: "No equity data",
    ja: "エクイティデータなし",
  },
  "backtests.runBacktest": {
    en: "Run test",
    ja: "テスト実行",
  },
  "backtests.creatingRun": {
    en: "Creating run…",
    ja: "実行作成中…",
  },
  "backtests.viewConfig": {
    en: "View config →",
    ja: "設定を見る →",
  },
  "backtests.configId": {
    en: "Config #",
    ja: "設定 #",
  },

  // -----------------------------------------------------------------------------
  // Backtest Diagnostics (Loss-focused, compliance-safe)
  // -----------------------------------------------------------------------------
  "backtests.diagnostics.title": {
    en: "Loss Diagnostics",
    ja: "損失診断",
  },
  "backtests.diagnostics.subtitle": {
    en: "Observational analysis of drawdown behavior and loss patterns.",
    ja: "ドローダウン挙動と損失パターンの観察分析。",
  },
  "backtests.diagnostics.noDataAvailable": {
    en: "No equity data available for diagnostics.",
    ja: "診断用のエクイティデータがありません。",
  },
  "backtests.diagnostics.noEquityData": {
    en: "Insufficient equity data",
    ja: "エクイティデータが不十分です",
  },
  "backtests.diagnostics.drawdownTimelineTitle": {
    en: "Drawdown over time",
    ja: "時系列ドローダウン",
  },
  "backtests.diagnostics.timeAxis": {
    en: "Time →",
    ja: "時間 →",
  },
  "backtests.diagnostics.drawdownAxis": {
    en: "Drawdown %",
    ja: "ドローダウン %",
  },
  "backtests.diagnostics.significantPeriods": {
    en: "Significant periods",
    ja: "重要な期間",
  },
  "backtests.diagnostics.clusteringDistributed": {
    en: "Losses distributed",
    ja: "損失は分散",
  },
  "backtests.diagnostics.clusteringLow": {
    en: "Minor clustering observed",
    ja: "軽度のクラスタリング",
  },
  "backtests.diagnostics.clusteringMedium": {
    en: "Moderate loss clustering",
    ja: "中程度の損失クラスタリング",
  },
  "backtests.diagnostics.clusteringHigh": {
    en: "High loss concentration",
    ja: "高い損失集中",
  },
  "backtests.diagnostics.longestStreak": {
    en: "Longest streak",
    ja: "最長連続",
  },
  "backtests.diagnostics.clusterCount": {
    en: "Clusters",
    ja: "クラスター数",
  },
  "backtests.diagnostics.sessionBreakdownTitle": {
    en: "Session breakdown (UTC)",
    ja: "セッション別内訳（UTC）",
  },
  "backtests.diagnostics.sessionTokyo": {
    en: "Tokyo",
    ja: "東京",
  },
  "backtests.diagnostics.sessionLondon": {
    en: "London",
    ja: "ロンドン",
  },
  "backtests.diagnostics.sessionNewYork": {
    en: "New York",
    ja: "ニューヨーク",
  },
  "backtests.diagnostics.periods": {
    en: "periods",
    ja: "期間",
  },
  "backtests.diagnostics.noSessionData": {
    en: "Session analysis requires timestamp data.",
    ja: "セッション分析にはタイムスタンプデータが必要です。",
  },
  "backtests.diagnostics.sessionDisclaimer": {
    en: "Session buckets are approximate (UTC). Actual market hours vary.",
    ja: "セッション区分は概算（UTC）です。実際の市場時間は異なります。",
  },
  "backtests.diagnostics.disclaimer": {
    en: "These diagnostics are observational only. They help identify patterns in historical test data but do not predict future behavior or guarantee outcomes.",
    ja: "これらの診断は観察目的のみです。過去のテストデータのパターン特定に役立ちますが、将来の挙動を予測したり結果を保証するものではありません。",
  },

  // -----------------------------------------------------------------------------
  // Backtest Run Detail (D.3 — MVP)
  // -----------------------------------------------------------------------------
  "backtests.run.title": {
    en: "Run",
    ja: "実行",
  },
  "backtests.run.statusQueued": {
    en: "Queued",
    ja: "キュー待ち",
  },
  "backtests.run.statusRunning": {
    en: "Running",
    ja: "実行中",
  },
  "backtests.run.statusCompleted": {
    en: "Completed",
    ja: "完了",
  },
  "backtests.run.statusFailed": {
    en: "Failed",
    ja: "失敗",
  },
  "backtests.run.startedAt": {
    en: "Started:",
    ja: "開始:",
  },
  "backtests.run.completedAt": {
    en: "Completed:",
    ja: "完了:",
  },
  "backtests.run.createdAt": {
    en: "Created:",
    ja: "作成日:",
  },
  "backtests.run.dataWindow": {
    en: "Data window",
    ja: "データ期間",
  },
  "backtests.run.noEquityCurve": {
    en: "No equity curve data available for this run.",
    ja: "この実行のエクイティカーブデータがありません。",
  },
  "backtests.run.noDataForRun": {
    en: "No detailed data available for this run.",
    ja: "この実行の詳細データがありません。",
  },
  "backtests.run.equityLabel": {
    en: "Equity",
    ja: "エクイティ",
  },
  "backtests.run.equityCurveLegend": {
    en: "Equity curve",
    ja: "エクイティカーブ",
  },
  "backtests.run.drawdownOverlayLegend": {
    en: "Drawdown (underwater)",
    ja: "ドローダウン（含み損）",
  },
  "backtests.run.chartsTitle": {
    en: "Behaviour during this run",
    ja: "この実行中の挙動",
  },
  "backtests.run.observedMetricsTitle": {
    en: "Observed metrics (based on available data)",
    ja: "観測指標（利用可能なデータに基づく）",
  },
  "backtests.run.longestDDDuration": {
    en: "Longest drawdown",
    ja: "最長ドローダウン",
  },
  "backtests.run.periods": {
    en: "periods",
    ja: "期間",
  },
  "backtests.run.totalTrades": {
    en: "Total trades",
    ja: "総取引数",
  },
  "backtests.run.metricsDisclaimer": {
    en: "Metrics shown are observational and based on historical test data during this run.",
    ja: "表示される指標は、この実行中の過去のテストデータに基づく観察値です。",
  },
  "backtests.run.lossObservationsTitle": {
    en: "Loss Observations",
    ja: "損失の観察",
  },
  "backtests.run.lossObservationsDisclaimer": {
    en: "These observations are heuristic patterns identified in historical data. They do not imply conclusions or advice.",
    ja: "これらの観察は過去データから特定されたヒューリスティックなパターンです。結論や助言を意味するものではありません。",
  },
  "backtests.run.obsHighLossFrequency": {
    en: "High frequency of loss periods observed during this run",
    ja: "この実行中に損失期間の頻度が高いことが観察されました",
  },
  "backtests.run.obsLowLossFrequency": {
    en: "Relatively low frequency of loss periods during this run",
    ja: "この実行中の損失期間の頻度は比較的低い",
  },
  "backtests.run.obsExtendedLossStreak": {
    en: "Extended consecutive loss periods observed",
    ja: "連続した損失期間の延長が観察されました",
  },
  "backtests.run.obsLossClustering": {
    en: "Losses concentrated in short periods",
    ja: "損失が短期間に集中",
  },
  "backtests.run.obsLossesDistributed": {
    en: "Losses distributed evenly across the run",
    ja: "損失が実行全体に均等に分布",
  },
  "backtests.run.obsExtendedDrawdownPhase": {
    en: "Extended drawdown phases observed",
    ja: "長期のドローダウン期間が観察されました",
  },
  "backtests.run.obsModerateDrawdownPhase": {
    en: "Moderate drawdown duration observed",
    ja: "中程度のドローダウン期間が観察されました",
  },
  "backtests.run.obsLargeLossMagnitude": {
    en: "Loss magnitude larger than gain magnitude on average",
    ja: "平均損失額が平均利益額より大きい",
  },
  "backtests.run.obsSmallLossMagnitude": {
    en: "Loss magnitude smaller than gain magnitude on average",
    ja: "平均損失額が平均利益額より小さい",
  },
  "backtests.run.obsSlowRecovery": {
    en: "Slow recovery patterns observed after drawdowns",
    ja: "ドローダウン後の回復が遅いパターンが観察されました",
  },
  "backtests.run.expandDetails": {
    en: "Show details",
    ja: "詳細を表示",
  },
  "backtests.run.collapseDetails": {
    en: "Hide details",
    ja: "詳細を非表示",
  },
  "backtests.run.runsCardTitle": {
    en: "Test Runs",
    ja: "テスト実行",
  },
  "backtests.run.noRunsYet": {
    en: "No runs found for this configuration yet.",
    ja: "この設定の実行はまだありません。",
  },
  "backtests.run.loadingRuns": {
    en: "Loading runs…",
    ja: "実行を読み込み中…",
  },
  "backtests.run.errorLabel": {
    en: "Error:",
    ja: "エラー:",
  },

  // -----------------------------------------------------------------------------
  // Backtest Demo Mode (Phase 1 — Confirmable Backtests)
  // -----------------------------------------------------------------------------
  "backtests.demoBadge": {
    en: "Demo",
    ja: "デモ",
  },
  "backtests.demoDisclaimer": {
    en: "Demo data — illustrative only, not real execution.",
    ja: "デモデータ — 例示のみ、実際の取引ではありません。",
  },
  "backtests.demoNote": {
    en: "Results shown are generated demo data for testing the platform. They do not represent real market execution or predict future outcomes.",
    ja: "表示される結果は、プラットフォームテスト用に生成されたデモデータです。実際の市場執行を示すものでも、将来の結果を予測するものでもありません。",
  },
  "backtests.run.demoLabel": {
    en: "Demo run",
    ja: "デモ実行",
  },
  "backtests.run.demoExplanation": {
    en: "This run used generated demo data, not actual market data or real execution.",
    ja: "この実行は生成されたデモデータを使用しており、実際の市場データや取引ではありません。",
  },

  // -----------------------------------------------------------------------------
  // Backtest Create Config Modal (Phase 1 — Confirmable Backtests)
  // -----------------------------------------------------------------------------
  "backtests.createConfig": {
    en: "Create test configuration",
    ja: "テスト設定を作成",
  },
  "backtests.createConfigTitle": {
    en: "New Test Configuration",
    ja: "新規テスト設定",
  },
  "backtests.createConfigSubtitle": {
    en: "Define parameters for a new test configuration. You can run tests after creation.",
    ja: "新しいテスト設定のパラメータを定義します。作成後にテストを実行できます。",
  },
  "backtests.form.nameLabel": {
    en: "Configuration name",
    ja: "設定名",
  },
  "backtests.form.namePlaceholder": {
    en: "e.g. EURUSD H1 Trend Test",
    ja: "例: EURUSD H1 トレンドテスト",
  },
  "backtests.form.descriptionLabel": {
    en: "Description (optional)",
    ja: "説明（任意）",
  },
  "backtests.form.descriptionPlaceholder": {
    en: "Brief description of this test configuration",
    ja: "このテスト設定の簡単な説明",
  },
  "backtests.form.strategyLabel": {
    en: "Strategy",
    ja: "戦略",
  },
  "backtests.form.selectStrategy": {
    en: "Select a strategy",
    ja: "戦略を選択",
  },
  "backtests.form.noStrategies": {
    en: "No strategies available. Create one first.",
    ja: "戦略がありません。先に作成してください。",
  },
  "backtests.form.symbolLabel": {
    en: "Symbol",
    ja: "シンボル",
  },
  "backtests.form.symbolPlaceholder": {
    en: "e.g. EURUSD",
    ja: "例: EURUSD",
  },
  "backtests.form.timeframeLabel": {
    en: "Timeframe",
    ja: "時間足",
  },
  "backtests.form.selectTimeframe": {
    en: "Select timeframe",
    ja: "時間足を選択",
  },
  "backtests.form.dateFromLabel": {
    en: "Start date",
    ja: "開始日",
  },
  "backtests.form.dateToLabel": {
    en: "End date",
    ja: "終了日",
  },
  "backtests.form.initialBalanceLabel": {
    en: "Initial balance",
    ja: "初期残高",
  },
  "backtests.form.initialBalancePlaceholder": {
    en: "e.g. 10000",
    ja: "例: 10000",
  },
  "backtests.form.cancel": {
    en: "Cancel",
    ja: "キャンセル",
  },
  "backtests.form.create": {
    en: "Create configuration",
    ja: "設定を作成",
  },
  "backtests.form.creating": {
    en: "Creating…",
    ja: "作成中…",
  },
  "backtests.form.success": {
    en: "Configuration created successfully.",
    ja: "設定が正常に作成されました。",
  },
  "backtests.form.prefillName": {
    en: "{strategy} — Test",
    ja: "{strategy} — テスト",
  },

  // Modal warning for incomplete strategy
  "backtests.modal.strategyIncompleteWarning": {
    en: "Strategy may be incomplete. Tests are informational only.",
    ja: "戦略が不完全な可能性があります。テストは情報提供のみを目的としています。",
  },
  "backtests.form.error": {
    en: "Failed to create configuration.",
    ja: "設定の作成に失敗しました。",
  },
  "backtests.runCreated": {
    en: "Test run created and queued.",
    ja: "テスト実行が作成され、キューに追加されました。",
  },
  "backtests.processedRuns": {
    en: "Processed {count} pending run(s).",
    ja: "{count}件の保留中の実行を処理しました。",
  },
  "backtests.headerHelpLine": {
    en: "Create a configuration to run a demo test. Results are illustrative only.",
    ja: "デモテストを実行するには設定を作成してください。結果は例示のみです。",
  },

  // -----------------------------------------------------------------------------
  // Strategy Control Page (Legal-first)
  // -----------------------------------------------------------------------------
  "strategy.control.title": {
    en: "Strategy Control",
    ja: "戦略コントロール",
  },
  "strategy.control.subtitle": {
    en: "Review strategy structure, verify readiness, and manage linked tests.",
    ja: "戦略の構造を確認し、準備状況を検証し、リンクされたテストを管理します。",
  },

  // Strategy Definition Section
  "strategy.definition.title": {
    en: "Strategy Definition",
    ja: "戦略定義",
  },
  "strategy.definition.subtitle": {
    en: "Structural parameters that define this strategy's behavior.",
    ja: "この戦略の動作を定義する構造パラメータ。",
  },
  "strategy.definition.nameLabel": {
    en: "Name",
    ja: "名前",
  },
  "strategy.definition.descriptionLabel": {
    en: "Description",
    ja: "説明",
  },
  "strategy.definition.noDescription": {
    en: "No description provided",
    ja: "説明なし",
  },
  "strategy.definition.styleLabel": {
    en: "Style",
    ja: "スタイル",
  },
  "strategy.definition.symbolsLabel": {
    en: "Symbols",
    ja: "シンボル",
  },
  "strategy.definition.timeframeLabel": {
    en: "Timeframe",
    ja: "時間足",
  },
  "strategy.definition.riskLabel": {
    en: "Risk per trade",
    ja: "1取引あたりのリスク",
  },
  "strategy.definition.magicLabel": {
    en: "Magic number",
    ja: "マジックナンバー",
  },
  "strategy.definition.entryLogicLabel": {
    en: "Entry logic",
    ja: "エントリーロジック",
  },
  "strategy.definition.exitLogicLabel": {
    en: "Exit logic",
    ja: "エグジットロジック",
  },
  "strategy.definition.notesLabel": {
    en: "Notes",
    ja: "メモ",
  },
  "strategy.definition.createdLabel": {
    en: "Created",
    ja: "作成日",
  },
  "strategy.definition.statusActive": {
    en: "Active",
    ja: "有効",
  },
  "strategy.definition.statusInactive": {
    en: "Inactive",
    ja: "無効",
  },

  // Readiness Checklist Section
  "strategy.readiness.title": {
    en: "Readiness Checklist",
    ja: "準備チェックリスト",
  },
  "strategy.readiness.subtitle": {
    en: "Verify these requirements before creating a test configuration.",
    ja: "テスト設定を作成する前にこれらの要件を確認してください。",
  },
  "strategy.readiness.hasName": {
    en: "Strategy has a name",
    ja: "戦略に名前がある",
  },
  "strategy.readiness.hasSymbol": {
    en: "At least one symbol defined",
    ja: "少なくとも1つのシンボルが定義されている",
  },
  "strategy.readiness.hasTimeframe": {
    en: "Timeframe specified",
    ja: "時間足が指定されている",
  },
  "strategy.readiness.hasEntryLogic": {
    en: "Entry logic defined",
    ja: "エントリーロジックが定義されている",
  },
  "strategy.readiness.hasExitLogic": {
    en: "Exit logic defined",
    ja: "エグジットロジックが定義されている",
  },
  "strategy.readiness.ready": {
    en: "Test-ready",
    ja: "テスト準備完了",
  },
  "strategy.readiness.notReady": {
    en: "Not ready",
    ja: "準備未完了",
  },
  "strategy.readiness.readyHint": {
    en: "This strategy meets the minimum requirements to create a test configuration.",
    ja: "この戦略はテスト設定を作成するための最低要件を満たしています。",
  },
  "strategy.readiness.notReadyHint": {
    en: "Complete the missing requirements above before creating a test.",
    ja: "テストを作成する前に上記の不足している要件を完了してください。",
  },

  // Linked Backtests Section
  "strategy.linkedBacktests.title": {
    en: "Linked Test Configurations",
    ja: "リンクされたテスト設定",
  },
  "strategy.linkedBacktests.subtitle": {
    en: "Test configurations associated with this strategy.",
    ja: "この戦略に関連付けられたテスト設定。",
  },
  "strategy.linkedBacktests.empty": {
    en: "No test configurations linked to this strategy yet.",
    ja: "この戦略にリンクされたテスト設定はまだありません。",
  },
  "strategy.linkedBacktests.emptyHint": {
    en: "Create a test configuration to observe how this strategy behaves on historical data.",
    ja: "過去データでこの戦略の動作を観察するためにテスト設定を作成してください。",
  },
  "strategy.linkedBacktests.symbolLabel": {
    en: "Symbol:",
    ja: "シンボル:",
  },
  "strategy.linkedBacktests.timeframeLabel": {
    en: "Timeframe:",
    ja: "時間足:",
  },
  "strategy.linkedBacktests.periodLabel": {
    en: "Period:",
    ja: "期間:",
  },
  "strategy.linkedBacktests.viewConfig": {
    en: "View →",
    ja: "表示 →",
  },
  "strategy.linkedBacktests.loading": {
    en: "Loading test configurations…",
    ja: "テスト設定を読み込み中…",
  },

  // Actions Section
  "strategy.actions.createBacktest": {
    en: "Create test configuration",
    ja: "テスト設定を作成",
  },
  "strategy.actions.createBacktestHint": {
    en: "Define parameters to run a test on historical data.",
    ja: "過去データでテストを実行するためのパラメータを定義します。",
  },
  "strategy.actions.openBuilder": {
    en: "Open strategy builder",
    ja: "戦略ビルダーを開く",
  },
  "strategy.actions.editComingSoon": {
    en: "Editing existing strategies is coming soon.",
    ja: "既存の戦略の編集機能は近日公開予定です。",
  },
  "strategy.actions.editStrategy": {
    en: "Edit Strategy",
    ja: "戦略を編集",
  },
  "strategy.actions.backToList": {
    en: "← All strategies",
    ja: "← 戦略一覧",
  },

  // Inline warning for incomplete strategy
  "strategy.testWarningInline": {
    en: "Some readiness checks are incomplete. You can still run informational tests.",
    ja: "一部の準備チェックが未完了です。情報提供目的のテストは実行できます。",
  },

  // Legal Disclaimer
  "strategy.disclaimer": {
    en: "Testing is informational only. Results depend on data quality and assumptions, and do not guarantee future outcomes.",
    ja: "テストは情報提供のみを目的としています。結果はデータの品質と仮定に依存し、将来の結果を保証するものではありません。",
  },

  // Engine Runtime Status Section
  "strategy.engineStatus.title": {
    en: "Engine Runtime Status",
    ja: "エンジンランタイムステータス",
  },
  "strategy.engineStatus.subtitle": {
    en: "Per-engine, per-symbol runtime metrics and risk counters.",
    ja: "エンジンごとのランタイムメトリクスとリスクカウンター。",
  },
  "strategy.engineEvents.title": {
    en: "Recent Evaluation Events",
    ja: "最近の評価イベント",
  },
  "strategy.engineEvents.subtitle": {
    en: "Signal evaluations, throttles, and regime changes.",
    ja: "シグナル評価、スロットル、レジーム変更。",
  },

  // -----------------------------------------------------------------------------
  // Strategy Edit Page
  // -----------------------------------------------------------------------------
  "strategyEdit.title": {
    en: "Edit Strategy",
    ja: "戦略を編集",
  },
  "strategyEdit.basicInfo": {
    en: "Basic Information",
    ja: "基本情報",
  },
  "strategyEdit.basicInfoSubtitle": {
    en: "Core strategy settings and identification.",
    ja: "コア戦略の設定と識別。",
  },
  "strategyEdit.enabled": {
    en: "Strategy Enabled",
    ja: "戦略有効",
  },
  "strategyEdit.tbpParameters": {
    en: "Trendline Break Pocket Parameters",
    ja: "トレンドラインブレイクポケットパラメータ",
  },
  "strategyEdit.tbpParametersSubtitle": {
    en: "Configure the HTF zone and trendline detection settings.",
    ja: "HTFゾーンとトレンドライン検出設定を構成。",
  },
  "strategyEdit.directionMode": {
    en: "Direction Mode",
    ja: "方向モード",
  },
  "strategyEdit.htfTimeframe": {
    en: "HTF Zone Timeframe",
    ja: "HTFゾーンタイムフレーム",
  },
  "strategyEdit.rrTarget": {
    en: "R:R Target",
    ja: "R:Rターゲット",
  },
  "strategyEdit.trendlineLookback": {
    en: "Trendline Lookback (bars)",
    ja: "トレンドラインルックバック（バー）",
  },
  "strategyEdit.pivotStrength": {
    en: "Pivot Strength",
    ja: "ピボット強度",
  },
  "strategyEdit.swingLookback": {
    en: "Swing Lookback",
    ja: "スイングルックバック",
  },
  "strategyEdit.maxTradesPerDay": {
    en: "Max Trades/Day",
    ja: "1日最大取引数",
  },
  "strategyEdit.newsFilterMode": {
    en: "News Filter",
    ja: "ニュースフィルター",
  },
  "strategyEdit.pocketRetestRequired": {
    en: "Pocket Retest Required",
    ja: "ポケットリテスト必須",
  },
  "strategyEdit.htfZones": {
    en: "HTF Zones",
    ja: "HTFゾーン",
  },
  "strategyEdit.htfZonesSubtitle": {
    en: "Define supply and demand zones for each symbol.",
    ja: "各シンボルの供給と需要ゾーンを定義。",
  },
  "strategyEdit.zonesSeededHint": {
    en: "Seeded defaults provided — edit as needed. Zones marked 'Seeded' are templates; edit them to customize.",
    ja: "シードされたデフォルトが提供されています — 必要に応じて編集してください。",
  },
  "strategyEdit.logicRules": {
    en: "Entry & Exit Logic",
    ja: "エントリーとエグジットロジック",
  },
  "strategyEdit.logicRulesSubtitle": {
    en: "Human-readable rules and notes for your trading plan.",
    ja: "取引計画の人間が読めるルールとメモ。",
  },
  "strategyEdit.save": {
    en: "Save Changes",
    ja: "変更を保存",
  },
  "strategyEdit.saving": {
    en: "Saving...",
    ja: "保存中...",
  },
  "strategyEdit.cancel": {
    en: "Cancel",
    ja: "キャンセル",
  },
  "strategyEdit.lastUpdated": {
    en: "Last updated",
    ja: "最終更新",
  },

  // -----------------------------------------------------------------------------
  // Live Trading Shell (Legal-first, execution disabled)
  // -----------------------------------------------------------------------------
  "liveTrading.title": {
    en: "Live Trading",
    ja: "ライブ取引",
  },
  "liveTrading.subtitle": {
    en: "View linked accounts and strategy assignments. Execution controls will be available in a future release.",
    ja: "連携された口座と戦略の割り当てを確認します。実行機能は将来のリリースで利用可能になります。",
  },
  "liveTrading.disclaimerLine1": {
    en: "This page is informational only. No trades are executed from this interface. GuvFX provides platform tools only — not financial advice.",
    ja: "このページは情報提供のみを目的としています。このインターフェースから取引は実行されません。GuvFXはプラットフォームツールのみを提供し、投資助言は行いません。",
  },
  "liveTrading.execDisabledTitle": {
    en: "Execution is disabled",
    ja: "実行は無効です",
  },
  "liveTrading.execDisabledBody": {
    en: "Trade execution functionality is not yet available. This page provides a read-only view of your accounts and strategy assignments. Automated execution controls are planned for a future release.",
    ja: "取引実行機能はまだ利用できません。このページでは口座と戦略の割り当てを読み取り専用で確認できます。自動実行機能は将来のリリースで予定されています。",
  },
  "liveTrading.accountsTitle": {
    en: "Linked Accounts",
    ja: "連携済み口座",
  },
  "liveTrading.accountsSubtitle": {
    en: "Trading accounts connected to your GuvFX profile.",
    ja: "GuvFXプロファイルに接続された取引口座。",
  },
  "liveTrading.accountsEmpty": {
    en: "No trading accounts linked yet.",
    ja: "連携された取引口座がまだありません。",
  },
  "liveTrading.strategiesTitle": {
    en: "Strategies",
    ja: "戦略",
  },
  "liveTrading.strategiesSubtitle": {
    en: "Strategies available for assignment to accounts.",
    ja: "口座に割り当て可能な戦略。",
  },
  "liveTrading.strategiesEmpty": {
    en: "No strategies created yet.",
    ja: "戦略がまだ作成されていません。",
  },
  "liveTrading.assignedStrategies": {
    en: "Assigned strategies",
    ja: "割り当て済み戦略",
  },
  "liveTrading.assigned": {
    en: "Assigned",
    ja: "割り当て済み",
  },
  "liveTrading.notAssigned": {
    en: "Not assigned",
    ja: "未割り当て",
  },
  "liveTrading.nextStepsTitle": {
    en: "Next Steps",
    ja: "次のステップ",
  },
  "liveTrading.nextStepsBody": {
    en: "Prepare your trading setup by linking accounts, creating strategies, and running tests on historical data.",
    ja: "口座を連携し、戦略を作成し、過去データでテストを実行して取引の準備を整えましょう。",
  },
  "liveTrading.ctaLinkAccount": {
    en: "Link account",
    ja: "口座を連携",
  },
  "liveTrading.ctaCreateStrategy": {
    en: "Create strategy",
    ja: "戦略を作成",
  },
  "liveTrading.ctaCreateTest": {
    en: "Create test",
    ja: "テストを作成",
  },
  "liveTrading.ctaViewBacktests": {
    en: "View backtests",
    ja: "バックテストを表示",
  },

  // -----------------------------------------------------------------------------
  // Trade History (Observational, Legal-first)
  // -----------------------------------------------------------------------------
  "tradeHistory.title": {
    en: "Trade History",
    ja: "取引履歴",
  },
  "tradeHistory.subtitle": {
    en: "Observed closed trades from linked accounts. Data is read-only and informational.",
    ja: "連携口座からの確定済み取引を表示します。データは読み取り専用で情報提供目的です。",
  },
  "tradeHistory.disclaimerLine1": {
    en: "Trade history is informational only. Results depend on execution conditions and do not guarantee future outcomes.",
    ja: "取引履歴は情報提供のみを目的としています。結果は執行条件に依存し、将来の結果を保証するものではありません。",
  },
  "tradeHistory.filterAccountLabel": {
    en: "Account",
    ja: "口座",
  },
  "tradeHistory.noAccountsOption": {
    en: "No accounts linked",
    ja: "連携された口座がありません",
  },
  "tradeHistory.refresh": {
    en: "Refresh",
    ja: "更新",
  },
  "tradeHistory.refreshing": {
    en: "Loading…",
    ja: "読み込み中…",
  },
  "tradeHistory.loading": {
    en: "Loading trade history…",
    ja: "取引履歴を読み込み中…",
  },
  "tradeHistory.emptyTitle": {
    en: "No trade history yet",
    ja: "取引履歴がまだありません",
  },
  "tradeHistory.emptyBody": {
    en: "Link a trading account and execute trades to see your history here. Trade data is synchronized automatically.",
    ja: "取引口座を連携し、取引を実行するとここに履歴が表示されます。取引データは自動的に同期されます。",
  },
  "tradeHistory.ctaLinkAccount": {
    en: "Link account",
    ja: "口座を連携",
  },
  "tradeHistory.ctaLiveTrading": {
    en: "View live trading",
    ja: "ライブ取引を表示",
  },
  "tradeHistory.sectionChartsTitle": {
    en: "Observed Patterns",
    ja: "観測されたパターン",
  },
  "tradeHistory.sectionChartsSubtitle": {
    en: "Visual representation of historical trade data. Charts show observed outcomes only.",
    ja: "過去の取引データの視覚的表現。チャートは観測された結果のみを表示します。",
  },
  "tradeHistory.chartEquityTitle": {
    en: "Balance trajectory (observed)",
    ja: "残高推移（観測値）",
  },
  "tradeHistory.chartOutcomesTitle": {
    en: "Outcome distribution (counts)",
    ja: "結果分布（件数）",
  },
  "tradeHistory.chartDrawdownTitle": {
    en: "Drawdown (underwater, observed)",
    ja: "ドローダウン（水面下、観測値）",
  },
  "tradeHistory.sectionDetailsTitle": {
    en: "Observed Statistics",
    ja: "観測された統計",
  },
  "tradeHistory.sectionDetailsSubtitle": {
    en: "Summary counts and metrics from historical data. These are observations, not predictions.",
    ja: "過去データからの集計と指標。これらは観察であり、予測ではありません。",
  },
  "tradeHistory.statTrades": {
    en: "Trades",
    ja: "取引数",
  },
  "tradeHistory.statObservedHitRate": {
    en: "Observed hit rate",
    ja: "観測されたヒット率",
  },
  "tradeHistory.statLongestLossStreak": {
    en: "Longest loss streak",
    ja: "最長連敗",
  },
  "tradeHistory.statMaxDrawdown": {
    en: "Max drawdown (observed)",
    ja: "最大ドローダウン（観測値）",
  },
  "tradeHistory.sectionTradesTitle": {
    en: "Trade Records",
    ja: "取引記録",
  },
  "tradeHistory.sectionTradesSubtitle": {
    en: "Individual closed trades from the selected account.",
    ja: "選択された口座の個別の確定取引。",
  },
  "tradeHistory.colTime": {
    en: "Time",
    ja: "時刻",
  },
  "tradeHistory.colTicket": {
    en: "Ticket",
    ja: "チケット",
  },
  "tradeHistory.colTickets": {
    en: "Tickets",
    ja: "チケット",
  },
  "tradeHistory.colSymbol": {
    en: "Symbol",
    ja: "シンボル",
  },
  "tradeHistory.colSide": {
    en: "Deal side",
    ja: "売買方向",
  },
  "tradeHistory.colType": {
    en: "Type",
    ja: "タイプ",
  },
  "tradeHistory.colVolume": {
    en: "Volume",
    ja: "数量",
  },
  "tradeHistory.colOutcome": {
    en: "Outcome",
    ja: "結果",
  },
  "tradeHistory.colStrategy": {
    en: "Strategy",
    ja: "戦略",
  },
  "tradeHistory.colTradeClosed": {
    en: "Trade Closed",
    ja: "取引終了",
  },
  "tradeHistory.colTradeNumbers": {
    en: "Trade Numbers",
    ja: "取引番号",
  },
  "tradeHistory.colDirection": {
    en: "Buy/Sell",
    ja: "売買",
  },
  "tradeHistory.statNetPnL": {
    en: "Net P&L (observed)",
    ja: "純損益（観測値）",
  },
  "tradeHistory.statMT5Balance": {
    en: "MT5 Balance",
    ja: "MT5残高",
  },

  // ===========================================================================
  // DASHBOARD - Action Tiles, System Status, Next Steps
  // ===========================================================================

  // Action Tiles
  "dashboard.tile.createStrategy.title": {
    en: "Create Strategy",
    ja: "戦略を作成",
  },
  "dashboard.tile.createStrategy.description": {
    en: "Define entry and exit rules for backtesting. Strategies are structural definitions only.",
    ja: "バックテスト用のエントリーおよびエグジットルールを定義します。戦略は構造的な定義のみです。",
  },
  "dashboard.tile.createStrategy.cta": {
    en: "New Strategy",
    ja: "新しい戦略",
  },
  "dashboard.tile.runBacktests.title": {
    en: "Run Backtests",
    ja: "バックテストを実行",
  },
  "dashboard.tile.runBacktests.description": {
    en: "Apply strategies to historical data and observe simulated outcomes.",
    ja: "戦略を過去データに適用し、シミュレーション結果を観察します。",
  },
  "dashboard.tile.runBacktests.cta": {
    en: "View Tests",
    ja: "テストを表示",
  },
  "dashboard.tile.liveTrading.title": {
    en: "Live Trading",
    ja: "ライブ取引",
  },
  "dashboard.tile.liveTrading.description": {
    en: "View linked accounts and strategy assignments. Execution features coming soon.",
    ja: "リンクされた口座と戦略の割り当てを表示します。実行機能は近日公開予定です。",
  },
  "dashboard.tile.liveTrading.cta": {
    en: "View Status",
    ja: "ステータスを表示",
  },
  "dashboard.tile.tradeHistory.title": {
    en: "Trade History",
    ja: "取引履歴",
  },
  "dashboard.tile.tradeHistory.description": {
    en: "Review observed trades from linked accounts. Informational display only.",
    ja: "リンクされた口座からの観察された取引を確認します。情報表示のみ。",
  },
  "dashboard.tile.tradeHistory.cta": {
    en: "View History",
    ja: "履歴を表示",
  },

  // System Status
  "dashboard.systemStatus.title": {
    en: "System Status",
    ja: "システムステータス",
  },
  "dashboard.systemStatus.subtitle": {
    en: "Current resource counts",
    ja: "現在のリソース数",
  },
  "dashboard.systemStatus.strategies": {
    en: "Strategies",
    ja: "戦略",
  },
  "dashboard.systemStatus.linkedAccounts": {
    en: "Linked Accounts",
    ja: "リンクされた口座",
  },
  "dashboard.systemStatus.testConfigs": {
    en: "Test Configs",
    ja: "テスト設定",
  },
  "dashboard.systemStatus.note": {
    en: "Counts reflect saved resources. These are structural summaries only.",
    ja: "カウントは保存されたリソースを反映しています。これらは構造的な概要のみです。",
  },

  // Next Steps
  "dashboard.nextSteps.title": {
    en: "Next Steps",
    ja: "次のステップ",
  },
  "dashboard.nextSteps.subtitle": {
    en: "Suggested workflow",
    ja: "推奨ワークフロー",
  },
  "dashboard.nextSteps.createStrategy": {
    en: "Create a strategy",
    ja: "戦略を作成する",
  },
  "dashboard.nextSteps.runTest": {
    en: "Run a backtest",
    ja: "バックテストを実行する",
  },
  "dashboard.nextSteps.reviewResults": {
    en: "Review test results",
    ja: "テスト結果を確認する",
  },
  "dashboard.nextSteps.linkAccount": {
    en: "Link a trading account",
    ja: "取引口座をリンクする",
  },
  "dashboard.nextSteps.note": {
    en: "Checklist reflects current status. No investment advice is implied.",
    ja: "チェックリストは現在のステータスを反映しています。投資アドバイスを意味するものではありません。",
  },

  // Quick Links
  "dashboard.quickLinks.label": {
    en: "Quick links:",
    ja: "クイックリンク：",
  },
  "dashboard.quickLinks.strategies": {
    en: "Strategies",
    ja: "戦略",
  },
  "dashboard.quickLinks.accounts": {
    en: "Accounts",
    ja: "口座",
  },
  "dashboard.quickLinks.profile": {
    en: "Profile",
    ja: "プロフィール",
  },

  // -----------------------------------------------------------------------------
  // Closed-beta critical journey
  // -----------------------------------------------------------------------------
  "enableModal.aria": { en: "Enable automated trading", ja: "自動売買を有効にする" },
  "enableModal.title": { en: "Enable automated trading?", ja: "自動売買を有効にしますか？" },
  "enableModal.body": {
    en: "This will allow GuvFX to place trades automatically on {account} using {strategy}. Trades will follow the strategy's signals until you turn it off. You can pause it at any time from My Strategies.",
    ja: "GuvFXが{account}で{strategy}のシグナルに従って自動的に取引します。自動売買は「利用中の戦略」からいつでも停止できます。",
  },
  "enableModal.demoNote": {
    en: "This action applies only to the selected demo account. Demo trading does not use real funds.",
    ja: "この操作は選択したデモ口座にのみ適用されます。デモ取引では実際の資金を使用しません。",
  },
  "common.cancel": { en: "Cancel", ja: "キャンセル" },
  "common.tryAgain": { en: "Try again", ja: "もう一度試す" },
  "common.configure": { en: "Configure", ja: "設定" },
  "common.manage": { en: "Manage", ja: "管理" },
  "common.enable": { en: "Enable", ja: "有効にする" },
  "common.enabling": { en: "Enabling…", ja: "有効化しています…" },
  "enableModal.confirm": { en: "Enable Strategy", ja: "戦略を有効にする" },

  "configure.row.account.label": { en: "Trading account", ja: "取引口座" },
  "configure.row.account.help": { en: "The demo account this strategy will trade on.", ja: "この戦略を運用するデモ口座です。" },
  "configure.row.strategy.label": { en: "Strategy", ja: "戦略" },
  "configure.row.provider.label": { en: "Signal provider", ja: "シグナル提供元" },
  "configure.row.instrument.label": { en: "Instrument", ja: "銘柄" },
  "configure.row.timeframe.label": { en: "Timeframe", ja: "時間足" },
  "configure.row.execution.label": { en: "Execution", ja: "取引方法" },
  "configure.row.execution.value": { en: "Automatically mirrors the provider's signals into your account", ja: "提供元のシグナルに従い、口座で自動的に取引します" },
  "configure.row.sizing.label": { en: "Position sizing", ja: "取引数量" },
  "configure.row.sizing.value": { en: "Set by you per position", ja: "ポジションごとにお客様が設定" },
  "configure.row.sizing.help": { en: "Set the lot size for each position below after adding the strategy.", ja: "戦略を追加した後、下の欄で各ポジションのロット数を設定できます。" },
  "configure.row.takeprofit.label": { en: "Take-profit", ja: "利益確定" },
  "configure.row.takeprofit.value": { en: "Follows the provider's targets", ja: "提供元の目標値に従います" },
  "configure.row.takeprofit.help": { en: "Take-profit levels come from the signal provider and can't be customised yet.", ja: "利益確定値はシグナル提供元が設定します。現在は変更できません。" },
  "configure.row.stoploss.label": { en: "Stop loss", ja: "損切り" },
  "configure.row.stoploss.value": { en: "Set by the provider's signal", ja: "提供元のシグナルで設定" },
  "configure.row.stoploss.help": { en: "The stop loss comes from the signal provider and can't be customised yet.", ja: "損切り値はシグナル提供元が設定します。現在は変更できません。" },
  "configure.row.trailing.label": { en: "Trailing stop", ja: "トレーリングストップ" },
  "configure.row.trailing.value": { en: "Not used by this strategy", ja: "この戦略では使用しません" },
  "configure.row.trailing.help": { en: "This strategy doesn't use a trailing stop.", ja: "この戦略ではトレーリングストップを使用しません。" },

  "myStrategies.title": { en: "My Strategies", ja: "利用中の戦略" },
  "myStrategies.subtitle": {
    en: "View and manage your strategies and automated trading settings.",
    ja: "戦略と自動売買の設定を確認・管理できます。",
  },
  "myStrategies.enabledSuccess": {
    en: "Your strategy is enabled. GuvFX will trade it automatically on your account.",
    ja: "戦略を有効にしました。GuvFXがこの口座で自動的に取引します。",
  },
  "myStrategies.automated": { en: "Automated strategies", ja: "自動売買戦略" },
  "myStrategies.state.readyToEnable": { en: "Ready to enable", ja: "有効化できます" },
  "myStrategies.state.setupRequired": { en: "Setup required", ja: "設定が必要です" },
  "myStrategies.state.needsAttention": { en: "Needs attention", ja: "確認が必要です" },
  "myStrategies.strategies": { en: "Strategies", ja: "戦略" },
  "myStrategies.create": { en: "Create strategy", ja: "戦略を作成" },
  "myStrategies.manageHelp": {
    en: "Manage your strategies and turn them on or off.",
    ja: "戦略の設定や有効・無効を管理できます。",
  },
  "myStrategies.loginRequired": {
    en: "Please log in to view your strategies.",
    ja: "戦略を表示するにはログインしてください。",
  },
  "myStrategies.loading": { en: "Loading strategies…", ja: "戦略を読み込んでいます…" },
  "myStrategies.empty": {
    en: "You don't have any strategies yet. Create one to get started.",
    ja: "戦略はまだありません。まず戦略を作成してください。",
  },
  "myStrategies.automatedBadge": { en: "Automated", ja: "自動売買" },
  "myStrategies.active": { en: "Active", ja: "有効" },
  "myStrategies.inactive": { en: "Inactive", ja: "無効" },
  "myStrategies.actions": { en: "Strategy actions", ja: "戦略の操作" },
  "myStrategies.actionsPlaceholder": { en: "Actions", ja: "操作" },
  "myStrategies.activate": { en: "Activate", ja: "有効にする" },
  "myStrategies.deactivate": { en: "Deactivate", ja: "無効にする" },
  "myStrategies.delete": { en: "Delete…", ja: "削除…" },
  "myStrategies.deleteConfirm": {
    en: "Delete strategy “{strategy}”? This cannot be undone.",
    ja: "戦略「{strategy}」を削除しますか？この操作は取り消せません。",
  },
  "myStrategies.actionFailed": {
    en: "We couldn't complete that action. Please try again.",
    ja: "操作を完了できませんでした。もう一度お試しください。",
  },
  "myStrategies.noDescription": { en: "No description", ja: "説明はありません" },
  "myStrategies.symbols": { en: "Symbols", ja: "銘柄" },
  "myStrategies.timeframe": { en: "Timeframe", ja: "時間足" },
  "myStrategies.engine": { en: "Engine", ja: "エンジン" },
  "myStrategies.created": { en: "Created", ja: "作成日時" },
  "myStrategies.viewDetails": {
    en: "View details and AI suggestions →",
    ja: "詳細とAIの提案を見る →",
  },

  "hostedStatus.title": { en: "Hosted Workspace", ja: "ホステッドワークスペース" },
  "hostedStatus.notSetUp": { en: "Not set up yet", ja: "未設定" },
  "hostedStatus.preparing": { en: "Preparing", ja: "準備中" },
  "hostedStatus.waitingLogin": { en: "Waiting for you to log in", ja: "ログインをお待ちしています" },
  "hostedStatus.confirmAccount": { en: "Confirm your account", ja: "口座を確認してください" },
  "hostedStatus.finishing": { en: "Finishing up", ja: "最終確認中" },
  "hostedStatus.ready": { en: "Ready", ja: "準備完了" },
  "hostedStatus.needsAttention": { en: "Needs attention", ja: "確認が必要です" },
  "hostedStatus.readyToOpen": { en: "Ready to open", ja: "起動できます" },
  "hostedStatus.notReady": { en: "Not ready yet", ja: "まだ準備中です" },
  "hostedStatus.actionNeeded": { en: "Action needed", ja: "操作が必要です" },
  "hostedStatus.inProgress": { en: "In progress", ja: "処理中" },
  "hostedStatus.workspaceLabel": { en: "Workspace status", ja: "ワークスペース" },
  "hostedStatus.terminalLabel": { en: "MetaTrader terminal", ja: "MetaTraderターミナル" },
  "hostedStatus.brokerLabel": { en: "Broker account", ja: "取引口座" },
  "hostedStatus.accountType": { en: "Account type", ja: "口座種別" },
  "hostedStatus.activeAccount": { en: "Active account", ja: "利用中の口座" },
  "hostedStatus.readiness": { en: "Trading readiness", ja: "取引の準備状況" },
  "hostedStatus.automated": { en: "Automated trading", ja: "自動売買" },
  "hostedStatus.live": { en: "Live", ja: "ライブ" },
  "hostedStatus.demo": { en: "Demo", ja: "デモ" },
  "hostedStatus.notYet": { en: "Not yet", ja: "未接続" },
  "hostedStatus.notConnected": { en: "Not connected yet", ja: "まだ接続されていません" },
  "hostedStatus.settingUp": { en: "Setting up", ja: "設定中" },
  "hostedStatus.readyChoose": { en: "Ready — choose a strategy", ja: "準備完了 — 戦略を選択してください" },
  "hostedStatus.enabled": { en: "Enabled", ja: "有効" },
  "hostedStatus.readyNotEnabled": { en: "Ready — not yet enabled", ja: "準備完了 — まだ有効ではありません" },
  "hostedStatus.openMetaTrader": { en: "Open MetaTrader", ja: "MetaTraderを開く" },
  "hostedStatus.continueSetup": { en: "Continue setup", ja: "設定を続ける" },
  "hostedStatus.chooseStrategy": { en: "Choose a strategy →", ja: "戦略を選ぶ →" },
  "hostedStatus.privacy": {
    en: "GuvFX runs MetaTrader for you. You log in inside MetaTrader, and GuvFX never sees or stores your broker password.",
    ja: "MetaTraderはGuvFXが運用します。ログインはMetaTrader内で行い、GuvFXがお客様の取引口座パスワードを閲覧・保存することはありません。",
  },
  "hostedStatus.automatedEnabled": { en: "Automated trading is enabled", ja: "自動売買は有効です" },
  "hostedStatus.automatedEnabledBody": {
    en: "GuvFX will run the strategies you enable within your safety limits. You can turn this off at any time.",
    ja: "GuvFXは設定された安全上限の範囲内で、有効にした戦略を実行します。自動売買はいつでも停止できます。",
  },
  "hostedStatus.enableBody": {
    en: "Your MetaTrader workspace is ready for automated trading. Enable automated trading when you want GuvFX to begin running your enabled strategies.",
    ja: "MetaTraderワークスペースの準備ができました。GuvFXに有効な戦略の運用を開始させる場合は、自動売買を有効にしてください。",
  },
  "hostedStatus.enableError": {
    en: "We couldn't enable automated trading just now. Please try again in a moment.",
    ja: "自動売買を有効にできませんでした。しばらくしてからもう一度お試しください。",
  },
  "hostedStatus.enable": { en: "Enable automated trading", ja: "自動売買を有効にする" },
  "hostedStatus.desc.noWorkspace": {
    en: "You don't have a hosted workspace yet. Continue setup to get a managed MetaTrader terminal.",
    ja: "ホステッド・ワークスペースはまだありません。設定を続けて、管理されたMetaTraderターミナルを準備してください。",
  },
  "hostedStatus.desc.requested": {
    en: "We're preparing your private MetaTrader workspace. This usually takes a few minutes.",
    ja: "お客様専用のMetaTraderワークスペースを準備しています。通常は数分で完了します。",
  },
  "hostedStatus.desc.preparing": {
    en: "We're setting up your private MetaTrader workspace. This usually takes a few minutes.",
    ja: "お客様専用のMetaTraderワークスペースを設定しています。通常は数分で完了します。",
  },
  "hostedStatus.desc.awaitingLogin": {
    en: "Your workspace is ready. Continue setup to point it at your broker account and log in — inside MetaTrader, never here.",
    ja: "ワークスペースの準備ができました。設定を続け、MetaTrader内で取引口座にログインしてください。",
  },
  "hostedStatus.desc.connected": {
    en: "Your workspace is open. Continue setup to finish signing in to the correct account.",
    ja: "ワークスペースを起動しました。設定を続けて、正しい口座へのログインを完了してください。",
  },
  "hostedStatus.desc.confirm": {
    en: "We found your broker account. Continue setup to confirm it.",
    ja: "取引口座が見つかりました。設定を続けて口座を確認してください。",
  },
  "hostedStatus.desc.bound": {
    en: "Your account is confirmed. We're completing the final checks.",
    ja: "口座を確認しました。最後の確認を行っています。",
  },
  "hostedStatus.desc.ready": {
    en: "Your hosted MT5 workspace is connected and ready. Choose a strategy to get started.",
    ja: "ホステッドMT5ワークスペースの接続が完了しました。戦略を選んで始めましょう。",
  },
  "hostedStatus.desc.unavailable": {
    en: "Your hosted workspace isn't available right now. Our team can help restore it.",
    ja: "現在、ホステッド・ワークスペースを利用できません。復旧についてサポートいたします。",
  },

  "configure.chooseTitle": { en: "Choose a strategy", ja: "戦略を選択" },
  "configure.chooseBody": { en: "Pick a strategy from the marketplace to configure it.", ja: "マーケットプレイスから設定する戦略を選んでください。" },
  "configure.browse": { en: "Browse strategies", ja: "戦略を見る" },
  "configure.signIn": { en: "Please sign in to configure this strategy.", ja: "この戦略を設定するにはログインしてください。" },
  "configure.goSignIn": { en: "Go to sign in", ja: "ログインへ" },
  "configure.backMarketplace": { en: "← Back to marketplace", ja: "← マーケットプレイスに戻る" },
  "configure.title": { en: "Configure {strategy}", ja: "{strategy}の設定" },
  "configure.free": { en: "Free", ja: "無料" },
  "configure.subtitle": { en: "Review the settings for this strategy, then enable it when you're ready.", ja: "戦略の設定を確認し、準備ができたら有効にしてください。" },
  "configure.loading": { en: "Loading…", ja: "読み込んでいます…" },
  "configure.settings": { en: "Strategy settings", ja: "戦略設定" },
  "configure.managed": { en: "Managed", ja: "GuvFX管理" },
  "configure.lot.label": { en: "Lot size per trade", ja: "1トレードあたりのロット数" },
  "configure.lot.unit": { en: "lots per position", ja: "ロット / ポジション" },
  "configure.lot.help": {
    en: "Wayond may open up to {legs} positions for one signal, so this is the size of EACH position — not the total. Maximum at this setting: {max} lots total.",
    ja: "Wayondは1つのシグナルで最大{legs}件のポジションを開くことがあります。これは合計ではなく、各ポジションのサイズです。この設定での最大合計: {max}ロット。",
  },
  "configure.lot.save": { en: "Save", ja: "保存" },
  "configure.lot.saving": { en: "Saving…", ja: "保存中…" },
  "configure.lot.saved": { en: "Saved", ja: "保存しました" },
  "configure.lot.error": { en: "Could not save. Please check the value and try again.", ja: "保存できませんでした。値を確認して再度お試しください。" },
  "configure.betaNote": { en: "For the beta, you set lot size per position. Take-profit and stop-loss follow the provider's signal, and this strategy does not use a trailing stop.", ja: "ベータ期間中、各ポジションのロット数はお客様が設定します。利益確定と損切りは提供元のシグナルに従い、この戦略ではトレーリングストップを使用しません。" },
  "configure.researchTitle": { en: "Research strategy", ja: "リサーチ戦略" },
  "configure.template": { en: "Template", ja: "テンプレート" },
  "configure.researchAdded": { en: "{strategy} has been added to your strategies{account}.", ja: "{strategy}を利用中の戦略{account}に追加しました。" },
  "configure.researchBody": { en: "This is a research template. It does not place trades automatically. Open it to review the rules, edit the settings and run a backtest. Automated trading is available on signal-copy strategies.", ja: "これはリサーチ用テンプレートであり、自動的に取引を行いません。ルールの確認、設定の編集、バックテストに利用できます。自動売買はシグナルコピー戦略で利用できます。" },
  "configure.browseMore": { en: "Browse more strategies", ja: "他の戦略を見る" },
  "configure.attentionTitle": { en: "This strategy needs attention", ja: "この戦略は確認が必要です" },
  "configure.attentionBody": { en: "We couldn't get this strategy ready. Please contact support and we'll help resolve it.", ja: "この戦略の準備を完了できませんでした。サポートまでご連絡ください。" },
  "configure.contactSupport": { en: "Contact support", ja: "サポートに連絡" },
  "configure.checkingTitle": { en: "Checking your strategy…", ja: "戦略の状態を確認しています…" },
  "configure.checkingBody": { en: "We're loading this strategy's status. It will update automatically in a moment.", ja: "戦略の状態を読み込んでいます。まもなく自動的に更新されます。" },
  "configure.goMyStrategies": { en: "Go to My Strategies", ja: "利用中の戦略へ" },
  "configure.accountMissingTitle": { en: "We couldn't find that account", ja: "口座が見つかりません" },
  "configure.accountMissingBody": { en: "The account for this strategy isn't available. Choose an account from the marketplace to continue.", ja: "この戦略に設定された口座を利用できません。マーケットプレイスから口座を選び直してください。" },
  "configure.chooseAccount": { en: "Choose an account", ja: "口座を選択" },
  "configure.chooseAccountBody": { en: "Pick the account you want to use this strategy on from the marketplace.", ja: "この戦略で使用する口座をマーケットプレイスから選んでください。" },
  "configure.addTitle": { en: "Add this strategy", ja: "この戦略を追加" },
  "configure.addBody": { en: "This strategy isn't added to {account} yet. Add it to continue.", ja: "この戦略はまだ{account}に追加されていません。続けるには戦略を追加してください。" },
  "configure.adding": { en: "Adding…", ja: "追加しています…" },
  "configure.getStrategy": { en: "Get Strategy", ja: "戦略を追加" },
  "configure.enabledTitle": { en: "Automated trading is enabled", ja: "自動売買は有効です" },
  "configure.enabledBody": { en: "{strategy} is running on {account}. It will place trades automatically until you pause it.", ja: "{strategy}は{account}で稼働中です。停止するまで自動的に取引します。" },
  "configure.working": { en: "Working…", ja: "処理しています…" },
  "configure.disable": { en: "Disable Strategy", ja: "戦略を停止する" },
  "configure.readyTitle": { en: "Ready to enable", ja: "有効化できます" },
  "configure.readyBody": { en: "{strategy} is added to {account}. Enable it to let GuvFX trade this strategy automatically on your account.", ja: "{strategy}を{account}に追加しました。有効にすると、GuvFXがこの戦略で自動的に取引します。" },
  "configure.finishTitle": { en: "Finish setting up your workspace", ja: "ワークスペースの設定を完了してください" },
  "configure.finishBody": { en: "Your workspace needs one more step before you can enable this strategy. Continue setup to finish.", ja: "この戦略を有効にする前に、ワークスペースの設定をもう1段階完了する必要があります。" },
  "configure.gettingReadyTitle": { en: "Your workspace is getting ready", ja: "ワークスペースを準備しています" },
  "configure.gettingReadyBody": { en: "We're finishing your trading workspace. Open MetaTrader and log in if needed. You can enable this strategy as soon as it is ready.", ja: "取引ワークスペースの最終設定を行っています。必要に応じてMetaTraderを開いてログインしてください。準備ができ次第、この戦略を有効にできます。" },
  "configure.autoUpdate": { en: "This page updates automatically. The Enable button will appear here when your workspace is ready.", ja: "このページは自動的に更新されます。ワークスペースの準備ができると、有効化ボタンが表示されます。" },
  "configure.pauseSuccess": { en: "Automated trading paused for this account.", ja: "この口座の自動売買を停止しました。" },
  "configure.pauseError": { en: "We couldn't pause the strategy just now. Please try again.", ja: "戦略を停止できませんでした。もう一度お試しください。" },
  "configure.addSuccess": { en: "Strategy added. You can enable it below.", ja: "戦略を追加しました。下のボタンから有効にできます。" },
  "configure.addAccountError": { en: "This account must be a demo account and active.", ja: "有効なデモ口座を選択してください。" },
  "configure.addUnavailable": { en: "This strategy isn't available for your account yet. Please contact support.", ja: "この口座ではまだ戦略を利用できません。サポートまでご連絡ください。" },
  "configure.addError": { en: "We couldn't add this strategy just now. Please try again.", ja: "戦略を追加できませんでした。もう一度お試しください。" },
  "configure.enableError": { en: "We couldn't enable the strategy just now. Please try again.", ja: "戦略を有効にできませんでした。もう一度お試しください。" },
  "configure.enablePreparing": { en: "Your account is still getting ready to trade. Please try again shortly.", ja: "口座の取引準備を続けています。しばらくしてからもう一度お試しください。" },
  "configure.notificationsTitle": { en: "Notifications", ja: "通知" },
  "configure.notificationsEnable": { en: "Enable Telegram notifications for this strategy", ja: "このストラテジーのTelegram通知を有効にする" },
  "configure.notificationsDetail": { en: "Receive permitted results and signal-safe progress. Live trade entries are never sent.", ja: "許可された取引結果と安全な進捗を受け取ります。取引開始情報は送信されません。" },
  "configure.notificationsConnectRequired": { en: "Connect Telegram to enable notifications.", ja: "通知を有効にするにはTelegramを接続してください。" },
  "configure.notificationsConnect": { en: "Connect Telegram", ja: "Telegramを接続" },
  "configure.notificationsError": { en: "We couldn't update this notification setting. Please try again.", ja: "通知設定を更新できませんでした。もう一度お試しください。" },
  "hostedJourney.progressAria": { en: "Onboarding progress", ja: "設定の進行状況" },
  "hostedJourney.brokerLinked": { en: "Broker account linked", ja: "取引口座を登録しました" },
  "hostedJourney.waitingLogin": { en: "Waiting for broker login", ja: "取引口座へのログイン待ち" },
  "hostedJourney.detecting": { en: "Detecting your account", ja: "口座を確認しています" },
  "hostedJourney.accountDetected": { en: "Account detected", ja: "口座を確認しました" },
  "hostedJourney.confirmAccount": { en: "Confirm your account", ja: "口座を確認" },
  "hostedJourney.workspaceReady": { en: "Workspace ready", ja: "ワークスペースの準備完了" },
  "hostedJourney.waitNormal": { en: "We've received your broker account information. We're setting up your secure MetaTrader workspace. Please remain on this page; we'll automatically continue when everything is ready.", ja: "取引口座の情報を受け付けました。安全なMetaTraderワークスペースを設定しています。このページを開いたままお待ちください。準備ができると自動的に次へ進みます。" },
  "hostedJourney.waitSlow": { en: "This is taking a little longer than expected. Your workspace is still being prepared. Keep this page open. You can safely refresh or contact support if it does not complete after several more minutes.", ja: "通常より時間がかかっています。ワークスペースは引き続き準備中です。このページを開いたままお待ちください。数分たっても完了しない場合は、ページを更新するかサポートへご連絡ください。" },
  "hostedJourney.workspaceRequested": { en: "Workspace requested", ja: "ワークスペースを受け付けました" },
  "hostedJourney.settingUp": { en: "Setting up your secure MetaTrader workspace", ja: "安全なMetaTraderワークスペースを設定中" },
  "hostedJourney.finalVerification": { en: "Final verification", ja: "最終確認" },
  "hostedJourney.keepOpen": { en: "Keep this page open. The Open MetaTrader button will appear here automatically when your workspace is ready.", ja: "このページを開いたままお待ちください。準備ができると「MetaTraderを開く」ボタンが自動的に表示されます。" },
  "hostedJourney.passwordLater": { en: "You'll enter your broker password later inside MetaTrader. We never ask for it here.", ja: "取引口座のパスワードは、後ほどMetaTrader内で入力します。この画面で入力を求めることはありません。" },
  "hostedJourney.notifyReady": { en: "Notify me when it's ready", ja: "準備ができたら通知する" },
  "hostedJourney.notifyReadySaving": { en: "Saving notification request…", ja: "通知リクエストを保存しています…" },
  "hostedJourney.notifyReadyRequested": { en: "We'll notify you once this workspace is ready.", ja: "ワークスペースの準備ができ次第お知らせします。" },
  "hostedJourney.notifyReadySent": { en: "Your workspace-ready notification was sent.", ja: "ワークスペース準備完了の通知を送信しました。" },
  "hostedJourney.notifyReadyConnect": { en: "Finish connecting Telegram; your one-shot request is already saved.", ja: "Telegramの接続を完了してください。1回限りの通知リクエストは保存されています。" },
  "hostedJourney.notifyReadyError": { en: "We couldn't save this request. Please try again.", ja: "通知リクエストを保存できませんでした。もう一度お試しください。" },
  "hostedJourney.openMetaTrader": { en: "Open MetaTrader", ja: "MetaTraderを開く" },
  "hostedJourney.loginInstruction": { en: "Log in below using your broker password. It is entered only inside MetaTrader and GuvFX never sees it.", ja: "下のMetaTraderで取引口座のパスワードを入力してログインしてください。パスワードをGuvFXが閲覧することはありません。" },
  "hostedJourney.usingMetaTrader": { en: "You're using MetaTrader", ja: "MetaTraderを使用中" },
  "hostedJourney.detectingAccount": { en: "Detecting your account…", ja: "口座を確認しています…" },
  "hostedJourney.detectedBody": { en: "We detected account {account} in your MetaTrader workspace. Your identity is already verified — please confirm this is your trading account to finish.", ja: "MetaTraderワークスペースで口座{account}を確認しました。設定を完了するため、ご自身の取引口座であることを確認してください。" },
  "hostedJourney.confirming": { en: "Confirming…", ja: "確認しています…" },
  "hostedJourney.confirmButton": { en: "I confirm this is my trading account", ja: "自分の取引口座であることを確認する" },
  "hostedJourney.readyBody": { en: "Your hosted MetaTrader workspace is fully connected. You can now choose your first strategy.", ja: "ホステッドMT5の接続が完了しました。最初の戦略を選択できます。" },
  "hostedJourney.chooseStrategy": { en: "Choose Strategy", ja: "戦略を選ぶ" },
  "hostedJourney.metaTraderOpen": { en: "MetaTrader open below", ja: "下にMetaTraderを表示中" },
  "hostedJourney.loading": { en: "Loading your workspace…", ja: "ワークスペースを読み込んでいます…" },
  "hostedJourney.unavailableTitle": { en: "Hosted workspace", ja: "ホステッドワークスペース" },
  "hostedJourney.unavailableBody": { en: "Your hosted trading workspace isn't available yet. We'll let you know when it is ready.", ja: "ホステッド取引ワークスペースはまだ利用できません。準備ができ次第お知らせします。" },
  "hostedJourney.loadError": { en: "We couldn't load your workspace status.", ja: "ワークスペースの状態を読み込めませんでした。" },
  "hostedJourney.brokerNumber": { en: "Broker account number", ja: "取引口座番号" },
  "hostedJourney.brokerServer": { en: "Broker server", ja: "ブローカーサーバー" },
  "hostedJourney.saving": { en: "Saving…", ja: "保存しています…" },
  "hostedJourney.saveDetails": { en: "Save my broker details", ja: "取引口座情報を保存" },
  "hostedJourney.passwordSafety": { en: "Enter your password only inside MetaTrader. GuvFX never receives or stores it.", ja: "パスワードはMetaTrader内でのみ入力してください。GuvFXが受信・保存することはありません。" },
  "hostedJourney.working": { en: "Working on it — this page updates automatically.", ja: "処理中です。このページは自動的に更新されます。" },
  "hostedJourney.requesting": { en: "Requesting…", ja: "リクエストしています…" },
  "hostedJourney.supportBody": { en: "Our team can help get this sorted for you.", ja: "サポートチームがお手伝いします。" },
  "hostedJourney.checkingTitle": { en: "Checking your account", ja: "口座を確認しています" },
  "hostedJourney.loginTitle": { en: "Log into your broker account", ja: "取引口座にログイン" },
  "hostedJourney.checkingBody": { en: "We're checking that the account in MetaTrader matches the account you linked. You don't need to do anything else.", ja: "MetaTraderにログインした口座が、登録した口座と一致するか確認しています。そのままお待ちください。" },
  "hostedJourney.loginBody": { en: "Log in inside MetaTrader. We'll verify your account and continue automatically.", ja: "MetaTrader内でログインしてください。口座を確認し、自動的に次へ進みます。" },
  "hostedJourney.loginHint": { en: "Keep this page open while we verify your account.", ja: "口座の確認中は、このページを開いたままお待ちください。" },
  "hostedJourney.correctTitle": { en: "Log into the account you linked", ja: "登録した口座にログインしてください" },
  "hostedJourney.correctBody": { en: "The account currently open in MetaTrader is not the account you told us. Log in to the correct account and we'll continue automatically.", ja: "MetaTraderで現在開いている口座が、登録した口座と一致しません。正しい口座にログインすると、自動的に次へ進みます。" },
  "hostedJourney.step.request": { en: "Request workspace", ja: "申込み" },
  "hostedJourney.step.preparing": { en: "Preparing workspace", ja: "準備中" },
  "hostedJourney.step.open": { en: "Open workspace", ja: "MetaTraderを開く" },
  "hostedJourney.step.confirm": { en: "Confirm your account", ja: "口座確認" },
  "hostedJourney.step.ready": { en: "Ready to trade", ja: "準備完了" },
  "hostedJourney.startTitle": { en: "Set up your trading workspace", ja: "取引ワークスペースを設定" },
  "hostedJourney.startBody": { en: "Request a private MetaTrader workspace to connect your broker account.", ja: "取引口座を接続するため、お客様専用のMetaTraderワークスペースを準備します。" },
  "hostedJourney.requestWorkspace": { en: "Request workspace", ja: "ワークスペースを申し込む" },
  "hostedJourney.openAndLogin": { en: "Open MetaTrader and log in", ja: "MetaTraderを開いてログイン" },
  "hostedJourney.contactSupport": { en: "Contact support", ja: "サポートに連絡" },
  "hostedJourney.fallbackTitle": { en: "Something needs attention", ja: "確認が必要です" },
  "hostedJourney.fallbackBody": { en: "We couldn't read your workspace status. Please contact support so we can help.", ja: "ワークスペースの状態を確認できませんでした。サポートまでご連絡ください。" },
  "hostedJourney.state.preparingTitle": { en: "Preparing your workspace", ja: "ワークスペースを準備しています" },
  "hostedJourney.state.requestedBody": { en: "Preparing your private MT5 workspace. This usually completes within a few minutes. Please keep this page open — we'll move you to the next step automatically.", ja: "お客様専用のMT5ワークスペースを準備しています。通常は数分で完了します。このページを開いたままお待ちください。準備ができると自動的に次へ進みます。" },
  "hostedJourney.state.preparingBody": { en: "We're building your private, isolated MT5 workspace. This usually completes within a few minutes. Next you'll open your workspace and log in. Please keep this page open — we'll move you on automatically.", ja: "お客様専用の独立したMT5ワークスペースを構築しています。通常は数分で完了します。次にワークスペースを開いてログインします。このページを開いたままお待ちください。準備ができると自動的に次へ進みます。" },
  "hostedJourney.state.loginTitle": { en: "Log in to your account", ja: "取引口座にログイン" },
  "hostedJourney.state.awaitingLoginBody": { en: "Enter your broker account number and server so we can connect the correct account. Then open your hosted MetaTrader terminal and log in there. Your password is entered only inside MetaTrader; GuvFX never sees or stores it.", ja: "正しい取引口座に接続するため、取引口座番号とブローカーサーバーを入力してください。その後、ホステッドMetaTraderを開いてログインします。パスワードはMetaTrader内でのみ入力し、GuvFXが閲覧・保存することはありません。" },
  "hostedJourney.state.connectedBody": { en: "We found account {account}. Open MetaTrader and make sure you're logged into that account.", ja: "口座{account}を確認しました。MetaTraderを開き、その口座にログインしていることを確認してください。" },
  "hostedJourney.state.confirmTitle": { en: "Confirm your account", ja: "取引口座を確認" },
  "hostedJourney.state.confirmBody": { en: "We found account {account}. If that's correct, confirm it to finish setting up your workspace.", ja: "口座{account}を確認しました。正しい場合は、確認してワークスペースの設定を完了してください。" },
  "hostedJourney.state.finishingTitle": { en: "Finishing up", ja: "最終設定中" },
  "hostedJourney.state.finishingBody": { en: "Your account is confirmed — we're finishing the last step. Please keep this page open; we'll continue automatically.", ja: "取引口座を確認しました。最終設定を行っています。このページを開いたままお待ちください。自動的に次へ進みます。" },
  "hostedJourney.state.readyTitle": { en: "Your workspace is ready", ja: "ワークスペースの準備ができました" },
  "hostedJourney.state.readyBody": { en: "Your hosted MT5 workspace is connected and ready. Choose a strategy to get started.", ja: "ホステッドMT5ワークスペースの接続と準備が完了しました。戦略を選んで開始できます。" },
  "hostedJourney.state.unavailableTitle": { en: "Workspace unavailable", ja: "ワークスペースを利用できません" },
  "hostedJourney.state.unavailableBody": { en: "Your hosted workspace isn't available right now. Our team can help restore it.", ja: "現在、ホステッドワークスペースを利用できません。復旧についてサポートいたします。" },
  "hostedJourney.brokerNumberPlaceholder": { en: "e.g. 1234567", ja: "例：1234567" },
  "hostedJourney.brokerServerPlaceholder": { en: "e.g. YourBroker-Demo", ja: "例：YourBroker-Demo" },
  "terminal.title": { en: "MT5 Terminal", ja: "MT5ターミナル" },
  "terminal.preparingRefresh": { en: "Preparing your MT5 terminal. If this persists, please refresh.", ja: "MT5ターミナルを準備しています。時間がかかる場合はページを更新してください。" },
  "terminal.description": { en: "Your persistent MT5 terminal for {account}. Log in with your broker credentials inside MetaTrader.", ja: "{account}専用のMT5ターミナルです。取引口座へのログインはMetaTrader内で行ってください。" },
  "terminal.focusHint": { en: "MT5 Terminal (RemoteApp) — click inside MT5, then type", ja: "MT5ターミナル（RemoteApp）— MT5内をクリックしてから入力してください" },
  "terminal.connected": { en: "Connected", ja: "接続済み" },
  "terminal.fullScreen": { en: "Full Screen", ja: "全画面表示" },
  "terminal.exitFullScreen": { en: "Exit Full Screen", ja: "全画面表示を終了" },
  "terminal.firstLaunchShort": { en: "First launch: MetaTrader is downloading your broker's instrument catalogue. It is ready when the chart list and symbols appear.", ja: "初回起動：MetaTraderがブローカーの銘柄情報を取得しています。チャートと銘柄が表示されると利用できます。" },
  "terminal.firstLaunchTitle": { en: "First launch:", ja: "初回起動：" },
  "terminal.firstLaunchBody": { en: "The first time you open this terminal, MetaTrader downloads your broker's instrument catalogue. This can take up to 5 minutes. Keep this page open. Later launches usually take only a few seconds.", ja: "初回起動時は、MetaTraderがブローカーの銘柄情報を取得するため、最大5分ほどかかる場合があります。このページを開いたままお待ちください。次回以降は通常、数秒で起動します。" },
  "terminal.opening": { en: "Opening…", ja: "開いています…" },
  "terminal.open": { en: "Open MT5 Terminal", ja: "MT5ターミナルを開く" },
  "terminal.preparing": { en: "Preparing your MT5 terminal. The first launch can take up to 5 minutes. Keep this page open.", ja: "MT5ターミナルを準備しています。初回は最大5分ほどかかる場合があります。このページを開いたままお待ちください。" },
  "terminal.notReady": { en: "Your MT5 terminal is still preparing. Keep this page open and it will open automatically when ready.", ja: "MT5ターミナルはまだ準備中です。このページを開いたままお待ちください。準備ができると自動的に開きます。" },
  "terminal.openError": { en: "We couldn't open the MT5 terminal. Please try again.", ja: "MT5ターミナルを開けませんでした。もう一度お試しください。" },
  "terminal.iframeTitle": { en: "MT5 Terminal", ja: "MT5ターミナル" },
  "terminalAccess.title": { en: "Terminal Access", ja: "ターミナルアクセス" },
  "terminalAccess.subtitle": { en: "Open and monitor your MT5 terminal.", ja: "MT5ターミナルを開いて状況を確認できます。" },
  "terminalAccess.restricted": { en: "This workspace is restricted to MT5 interaction only.", ja: "このワークスペースではMT5の操作のみ利用できます。" },
  "terminalAccess.trading": { en: "Trading", ja: "取引" },
  "terminalAccess.viewer": { en: "Viewer", ja: "画面接続" },
  "terminalAccess.trading.healthy": { en: "Healthy", ja: "正常" },
  "terminalAccess.trading.warning": { en: "Warning", ja: "注意" },
  "terminalAccess.trading.critical": { en: "Critical", ja: "重大" },
  "terminalAccess.trading.unknown": { en: "Unknown", ja: "不明" },
  "terminalAccess.viewer.connected": { en: "Connected", ja: "接続済み" },
  "terminalAccess.viewer.connecting": { en: "Connecting", ja: "接続中" },
  "terminalAccess.viewer.reconnecting": { en: "Reconnecting", ja: "再接続中" },
  "terminalAccess.viewer.disconnected": { en: "Disconnected", ja: "未接続" },
  "terminalAccess.viewer.error": { en: "Error", ja: "エラー" },
  "terminalAccess.runtimeStatus": { en: "MT5 Runtime Status", ja: "MT5稼働状況" },
  "terminalAccess.login": { en: "Login", ja: "ログイン番号" },
  "terminalAccess.server": { en: "Server", ja: "サーバー" },
  "terminalAccess.status": { en: "Status", ja: "状態" },
  "terminalAccess.lastError": { en: "Last error", ja: "最新のエラー" },
  "terminalAccess.credentialError": { en: "The latest credential check needs attention.", ja: "最新の認証情報確認に対応が必要です。" },
  "terminalAccess.verified": { en: "Verified", ja: "確認日時" },
  "terminalAccess.credential.success": { en: "Verified", ja: "確認済み" },
  "terminalAccess.credential.failed": { en: "Needs attention", ja: "要確認" },
  "terminalAccess.credential.pending": { en: "Checking", ja: "確認中" },
  "terminalAccess.credential.never": { en: "Not checked", ja: "未確認" },
  "terminalAccess.credential.timeout": { en: "Check timed out", ja: "確認がタイムアウトしました" },
  "terminalAccess.credential.unknown": { en: "Unknown", ja: "不明" },
  "terminalAccess.opensAbove": { en: "Your MT5 terminal opens from the terminal card above.", ja: "上のターミナルカードからMT5ターミナルを開けます。" },
  "terminalAccess.launching": { en: "Launching…", ja: "起動しています…" },
  "terminalAccess.launchDesktop": { en: "Launch MT5 Desktop", ja: "MT5デスクトップを起動" },
  "terminalAccess.validateFirst": { en: "Validate credentials before launching.", ja: "起動する前に認証情報を確認してください。" },
  "terminalAccess.loadingSession": { en: "Loading session…", ja: "セッションを読み込んでいます…" },
  "terminalAccess.availableTerminals": { en: "Available Terminals", ja: "利用可能なターミナル" },
  "terminalAccess.refresh": { en: "Refresh", ja: "更新" },
  "terminalAccess.loadingTerminals": { en: "Loading terminals…", ja: "ターミナルを読み込んでいます…" },
  "terminalAccess.noTerminals": { en: "No terminals are available.", ja: "利用可能なターミナルはありません。" },
  "terminalAccess.noTerminalsBody": { en: "You may not have an active terminal authorization, or no terminal is currently configured.", ja: "有効なターミナル利用権限がないか、現在ターミナルが設定されていない可能性があります。" },
  "terminalAccess.sharedView": { en: "Shared View", ja: "共有表示" },
  "terminalAccess.currentSession": { en: "Current session", ja: "現在のセッション" },
  "terminalAccess.launch": { en: "Launch", ja: "起動" },
  "terminalAccess.inUse": { en: "In use", ja: "使用中" },
  "terminalAccess.suspended": { en: "Suspended", ja: "一時停止" },
  "terminalAccess.maintenance": { en: "Maintenance", ja: "メンテナンス中" },
  "terminalAccess.locked": { en: "Locked", ja: "ロック中" },
  "terminalAccess.busy": { en: "Busy", ja: "使用中" },
  "common.loading": { en: "Loading…", ja: "読み込んでいます…" },
  "onboarding.gettingStarted": { en: "Getting Started", ja: "はじめに" },
  "onboarding.loadingProgress": { en: "Loading your setup progress…", ja: "設定状況を読み込んでいます…" },
  "onboarding.loadError": { en: "We couldn't load your setup progress. Please try again.", ja: "設定状況を読み込めませんでした。もう一度お試しください。" },
  "onboarding.finishRemaining": { en: "Please finish the remaining setup steps before continuing.", ja: "続ける前に、残りの設定を完了してください。" },
  "onboarding.completeError": { en: "We couldn't complete setup. Please try again.", ja: "設定を完了できませんでした。もう一度お試しください。" },
  "onboarding.stepSummary": { en: "Step {step} of {total} — Complete the steps below to set up your GuvFX workspace.", ja: "ステップ {step}/{total} — 以下の手順でGuvFXワークスペースを設定します。" },
  "onboarding.selectPlan": { en: "Select Your Plan", ja: "プランを選択" },
  "onboarding.planConfirmed": { en: "Your plan has been confirmed.", ja: "プランを確定しました。" },
  "onboarding.planIntro": { en: "Choose a plan to get started. You can change it later from Billing.", ja: "利用するプランを選んでください。プランは後から請求設定で変更できます。" },
  "onboarding.plan.standard": { en: "Standard", ja: "スタンダード" },
  "onboarding.plan.standardDesc": { en: "Full platform access including backtests, strategy deployment, and live execution.", ja: "バックテスト、戦略の設定、ライブ運用を含むすべての機能を利用できます。" },
  "onboarding.plan.trial": { en: "Starter Trial", ja: "スタータートライアル" },
  "onboarding.plan.trialDesc": { en: "Limited access to explore the platform, backtests, and marketplace.", ja: "プラットフォーム、バックテスト、マーケットプレイスをお試しいただけます。" },
  "onboarding.recommended": { en: "Recommended", ja: "おすすめ" },
  "onboarding.activating": { en: "Activating…", ja: "有効化しています…" },
  "onboarding.activatePlan": { en: "Activate Plan", ja: "このプランを選択" },
  "onboarding.planError": { en: "We couldn't select that plan. Please try again.", ja: "プランを選択できませんでした。もう一度お試しください。" },
  "onboarding.accountReady": { en: "Your GuvFX account is ready.", ja: "GuvFXアカウントの準備ができました。" },
  "onboarding.accountReadyBody": { en: "Next, set up your private trading workspace. MetaTrader runs for you, and you log in inside it. GuvFX never sees your password.", ja: "次に、お客様専用の取引ワークスペースを設定します。MetaTrader内でログインするため、GuvFXがお客様のパスワードを閲覧することはありません。" },
  "onboarding.profileTitle": { en: "Complete Profile", ja: "プロフィール設定" },
  "onboarding.profileComplete": { en: "Profile setup is complete.", ja: "プロフィール設定が完了しました。" },
  "onboarding.selfManagedBody": { en: "Already trade with your own broker? You can connect an MT5 account you manage yourself instead.", ja: "ご自身で管理する取引口座をお持ちの場合は、そのMT5口座を接続することもできます。" },
  "onboarding.finishing": { en: "Finishing…", ja: "完了処理中…" },
  "onboarding.connectOwnBroker": { en: "Connect your own broker", ja: "自分の取引口座を接続" },
  "onboarding.broker.title": { en: "Connect a Broker Account", ja: "取引口座を接続" },
  "onboarding.broker.body": { en: "To trade on GuvFX, you need a broker account with MT5 access. You can connect an existing account or open one with a partner broker below.", ja: "GuvFXで取引するには、MT5を利用できる取引口座が必要です。既存の口座を接続するか、以下の提携ブローカーで口座を開設できます。" },
  "onboarding.broker.loading": { en: "Loading partner brokers…", ja: "提携ブローカーを読み込んでいます…" },
  "onboarding.broker.loadError": { en: "We couldn't load partner brokers. Please try again.", ja: "提携ブローカーを読み込めませんでした。もう一度お試しください。" },
  "onboarding.broker.referralTracked": { en: "Referral tracked", ja: "紹介を記録しました" },
  "onboarding.broker.openAccount": { en: "Open account", ja: "口座を開設" },
  "onboarding.broker.existingPrefix": { en: "Already have a broker account?", ja: "すでに取引口座をお持ちですか？" },
  "onboarding.broker.accountsLink": { en: "Connect it on the Broker Accounts page", ja: "取引口座ページで接続し" },
  "onboarding.broker.existingSuffix": { en: "then return here to confirm below.", ja: "このページに戻って以下を確認してください。" },
  "onboarding.connection.title": { en: "Account Connection", ja: "口座接続" },
  "onboarding.connection.connected": { en: "Your trading account is connected.", ja: "取引口座を接続しました。" },
  "onboarding.connection.connecting": { en: "Connecting your trading account", ja: "取引口座に接続しています" },
  "onboarding.connection.finishingTitle": { en: "Almost there — finishing setup…", ja: "まもなく完了します — 最終設定中…" },
  "onboarding.connection.finishingBody": { en: "Your terminal is up; we're running the final readiness checks. This page advances on its own.", ja: "ターミナルが起動しました。最終確認が完了すると、このページは自動的に次へ進みます。" },
  "onboarding.connection.settingUpTitle": { en: "Setting up your dedicated trading terminal…", ja: "専用取引ターミナルを設定しています…" },
  "onboarding.connection.settingUpBody": { en: "This usually takes a minute or two. Keep this page open — it updates automatically.", ja: "通常1〜2分で完了します。このページを開いたままお待ちください。表示は自動的に更新されます。" },
  "onboarding.connection.reconnectingTitle": { en: "Reconnecting your terminal…", ja: "ターミナルに再接続しています…" },
  "onboarding.connection.reconnectingBody": { en: "Your terminal needs a moment to recover. We're handling it automatically — no action needed.", ja: "ターミナルの復旧処理を自動で行っています。操作は不要です。" },
  "onboarding.connection.queuedTitle": { en: "Your terminal is queued.", ja: "ターミナルは設定待ちです。" },
  "onboarding.connection.queuedBody": { en: "All setup slots are busy right now. Yours will start automatically as soon as one frees up.", ja: "現在、設定枠がすべて使用中です。空き次第、自動的に開始します。" },
  "onboarding.connection.pausedTitle": { en: "Your terminal is paused.", ja: "ターミナルは一時停止中です。" },
  "onboarding.connection.pausedBody": { en: "It will resume automatically. If it stays paused, contact support.", ja: "自動的に再開します。停止状態が続く場合はサポートへお問い合わせください。" },
  "onboarding.connection.failedTitle": { en: "We hit a problem setting up your terminal.", ja: "ターミナルの設定中に問題が発生しました。" },
  "onboarding.connection.failedBody": { en: "Please try again shortly, or contact support if this persists.", ja: "しばらくしてからもう一度お試しください。問題が続く場合はサポートへお問い合わせください。" },
  "onboarding.connection.waitingTitle": { en: "Waiting to start setup…", ja: "設定開始を待っています…" },
  "onboarding.connection.waitingBody": { en: "Once your broker account is saved on the Broker Accounts page, we begin setting up your terminal automatically.", ja: "取引口座ページで口座を保存すると、ターミナルの設定が自動的に始まります。" },
  "onboarding.connection.pendingPrefix": { en: "Haven't added your account yet? Add it on the", ja: "まだ口座を追加していない場合は、" },
  "onboarding.connection.pendingSuffix": { en: "page — setup starts automatically.", ja: "ページで追加してください。設定は自動的に始まります。" },
  "onboarding.connection.checking": { en: "Checking…", ja: "確認しています…" },
  "onboarding.connection.checkAgain": { en: "Check again", ja: "もう一度確認" },
  "onboarding.connection.phase.account_received.title": { en: "Account received", ja: "口座情報を受け付けました" },
  "onboarding.connection.phase.account_received.body": { en: "We've recorded your broker account. Setup of your dedicated terminal begins automatically.", ja: "取引口座を登録しました。専用ターミナルの設定が自動的に始まります。" },
  "onboarding.connection.phase.provisioning_runtime.title": { en: "Preparing your terminal", ja: "ターミナルを準備しています" },
  "onboarding.connection.phase.provisioning_runtime.body": { en: "We're preparing your dedicated trading terminal. This usually takes a minute or two.", ja: "専用取引ターミナルを準備しています。通常1〜2分で完了します。" },
  "onboarding.connection.phase.connecting_broker.title": { en: "Connecting to your broker", ja: "ブローカーに接続しています" },
  "onboarding.connection.phase.connecting_broker.body": { en: "Your terminal is up — we're validating the broker connection.", ja: "ターミナルが起動しました。ブローカーへの接続を確認しています。" },
  "onboarding.connection.phase.validated.title": { en: "Connection confirmed", ja: "接続を確認しました" },
  "onboarding.connection.phase.validated.body": { en: "Your dedicated terminal is ready.", ja: "専用ターミナルの準備ができました。" },
  "onboarding.connection.phase.connection_failed.title": { en: "Connection failed", ja: "接続できませんでした" },
  "onboarding.connection.phase.connection_failed.body": { en: "We couldn't complete setup. Check your login details and try again.", ja: "設定を完了できませんでした。ログイン情報を確認し、もう一度お試しください。" },
  "onboarding.connection.step.account_received": { en: "Account received", ja: "口座受付" },
  "onboarding.connection.step.provisioning_runtime": { en: "Preparing terminal", ja: "ターミナル準備" },
  "onboarding.connection.step.connecting_broker": { en: "Connecting to broker", ja: "ブローカー接続" },
  "onboarding.connection.step.validated": { en: "Connection confirmed", ja: "接続確認済み" },
  "onboarding.readiness.title": { en: "Readiness Review", ja: "利用準備の確認" },
  "onboarding.readiness.body": { en: "Review your platform readiness. All checks below must pass before your strategies can run in the live environment.", ja: "プラットフォームの利用準備を確認します。ライブ環境で戦略を運用するには、以下の確認をすべて完了する必要があります。" },
  "onboarding.readiness.loading": { en: "Loading readiness status…", ja: "利用準備の状況を読み込んでいます…" },
  "onboarding.readiness.loadError": { en: "We couldn't load readiness status. Please try again.", ja: "利用準備の状況を読み込めませんでした。もう一度お試しください。" },
  "onboarding.readiness.onboarding": { en: "Onboarding", ja: "初期設定" },
  "onboarding.readiness.complete": { en: "Complete", ja: "完了" },
  "onboarding.readiness.incomplete": { en: "Incomplete", ja: "未完了" },
  "onboarding.readiness.missing": { en: "{count} setup step(s) remaining.", ja: "残りの設定ステップ：{count}件" },
  "onboarding.readiness.activeAccount": { en: "Active Trading Account", ja: "有効な取引口座" },
  "onboarding.readiness.liveAssignment": { en: "Live Strategy Assignment", ja: "ライブ戦略の割り当て" },
  "onboarding.readiness.validEntitlement": { en: "Valid Entitlement", ja: "有効な利用権限" },
  "onboarding.readiness.terminalAvailable": { en: "Terminal Available", ja: "ターミナル利用可" },
  "onboarding.readiness.additionalCheck": { en: "Additional readiness check", ja: "その他の利用準備確認" },
  "onboarding.readiness.pass": { en: "Pass", ja: "合格" },
  "onboarding.readiness.fail": { en: "Needs attention", ja: "要確認" },
  "onboarding.readiness.ready": { en: "Platform Ready — All checks passed", ja: "利用準備完了 — すべての確認に合格しました" },
  "onboarding.readiness.notReady": { en: "Not Yet Ready — Complete the remaining steps and checks", ja: "準備未完了 — 残りの設定と確認を完了してください" },
  "onboarding.strategy.title": { en: "Strategy Assignment", ja: "戦略の割り当て" },
  "onboarding.strategy.assigned": { en: "A strategy is assigned to your account.", ja: "口座に戦略が割り当てられています。" },
  "onboarding.strategy.assignTitle": { en: "Assign a Strategy", ja: "戦略を割り当て" },
  "onboarding.strategy.bodyPrefix": { en: "Create and assign a strategy to your trading account. Visit", ja: "取引口座に戦略を作成して割り当てます。" },
  "onboarding.strategy.bodySuffix": { en: "to create a strategy and assign it to your account, then return here to confirm.", ja: "で戦略を作成して口座に割り当てた後、ここに戻って確認してください。" },
  "onboarding.strategy.confirming": { en: "Confirming…", ja: "確認しています…" },
  "onboarding.strategy.confirm": { en: "Confirm Strategy Assignment", ja: "戦略の割り当てを確認" },
  "onboarding.strategy.confirmError": { en: "We couldn't confirm the strategy assignment. Please try again.", ja: "戦略の割り当てを確認できませんでした。もう一度お試しください。" },
  "onboarding.email.title": { en: "Email Verification", ja: "メール認証" },
  "onboarding.email.verified": { en: "Your email has been verified.", ja: "メールアドレスを確認しました。" },
  "onboarding.email.verifyTitle": { en: "Verify Your Email", ja: "メールアドレスを確認" },
  "onboarding.email.verifyBody": { en: "We need to verify your email address to proceed. Click below to receive a verification code.", ja: "続行するにはメールアドレスの確認が必要です。下のボタンから確認コードを受け取ってください。" },
  "onboarding.email.sendError": { en: "We couldn't send the verification email. Please try again.", ja: "確認メールを送信できませんでした。もう一度お試しください。" },
  "onboarding.email.verifyError": { en: "We couldn't verify that code. Please try again.", ja: "確認コードを認証できませんでした。もう一度お試しください。" },
  "onboarding.email.expired": { en: "That verification code has expired. Request a new code.", ja: "確認コードの有効期限が切れています。新しいコードを取得してください。" },
  "onboarding.email.used": { en: "That verification code has already been used. Request a new code.", ja: "この確認コードは使用済みです。新しいコードを取得してください。" },
  "onboarding.email.invalid": { en: "That verification code isn't valid. Check it and try again.", ja: "確認コードが正しくありません。コードを確認して、もう一度お試しください。" },
  "onboarding.email.sending": { en: "Sending…", ja: "送信中…" },
  "onboarding.email.sendCode": { en: "Send Verification Code", ja: "確認コードを送信" },
  "onboarding.email.sent": { en: "Verification code sent. Check your email and enter the code below.", ja: "確認コードを送信しました。メールを確認し、下にコードを入力してください。" },
  "onboarding.email.placeholder": { en: "Enter verification code", ja: "確認コードを入力" },
  "onboarding.email.verifying": { en: "Verifying…", ja: "確認中…" },
  "onboarding.email.verify": { en: "Verify", ja: "確認する" },
  "onboarding.email.resending": { en: "Resending…", ja: "再送信中…" },
  "onboarding.email.resend": { en: "Resend Code", ja: "コードを再送信" },
  "onboarding.twoFactor.title": { en: "Two-Factor Authentication", ja: "二要素認証" },
  "onboarding.twoFactor.enabled": { en: "2FA is enabled on your account.", ja: "二要素認証が有効です。" },
  "onboarding.twoFactor.body": { en: "Add an extra layer of security to your account with TOTP-based two-factor authentication. This step is optional — you can skip it and enable it later.", ja: "認証アプリを使う二要素認証で、アカウントの安全性を高めます。この設定は任意で、後から有効にすることもできます。" },
  "onboarding.twoFactor.setupError": { en: "We couldn't set up two-factor authentication. Please try again.", ja: "二要素認証を設定できませんでした。もう一度お試しください。" },
  "onboarding.twoFactor.invalid": { en: "That code isn't valid. Check it and try again.", ja: "認証コードが正しくありません。コードを確認して、もう一度お試しください。" },
  "onboarding.twoFactor.settingUp": { en: "Setting up…", ja: "設定中…" },
  "onboarding.twoFactor.setup": { en: "Set Up 2FA", ja: "二要素認証を設定" },
  "onboarding.twoFactor.skip": { en: "Skip for Now", ja: "今はスキップ" },
  "onboarding.twoFactor.scan": { en: "Scan the QR code or enter the secret in your authenticator app:", ja: "認証アプリでQRコードを読み取るか、シークレットキーを入力してください。" },
  "onboarding.twoFactor.placeholder": { en: "Enter 6-digit code", ja: "6桁のコードを入力" },
  "onboarding.twoFactor.verifying": { en: "Verifying…", ja: "確認中…" },
  "onboarding.twoFactor.verify": { en: "Verify", ja: "確認する" },
  "onboarding.risk.title": { en: "Risk Disclosure", ja: "リスクに関する重要事項" },
  "onboarding.risk.accepted": { en: "Risk disclosure accepted.", ja: "リスクに関する重要事項に同意しました。" },
  "onboarding.risk.acceptedOn": { en: "Risk disclosure accepted on {date}.", ja: "{date}にリスクに関する重要事項へ同意しました。" },
  "onboarding.risk.bodyOne": { en: "Trading in financial instruments carries a high level of risk and may not be suitable for all investors. You should carefully consider your investment objectives, level of experience, and risk appetite before making any trading decisions.", ja: "金融商品の取引には高いリスクがあり、すべての方に適しているとは限りません。取引を判断する前に、投資目的、取引経験、許容できるリスクを十分にご確認ください。" },
  "onboarding.risk.bodyTwo": { en: "Past performance does not guarantee future results. GuvFX is a strategy management platform and does not provide investment advice. You are solely responsible for all trading decisions made through this platform.", ja: "過去の実績は将来の成果を保証するものではありません。GuvFXは戦略管理プラットフォームであり、投資助言を提供しません。本プラットフォームを通じた取引の判断と結果は、お客様ご自身の責任となります。" },
  "onboarding.risk.saveError": { en: "We couldn't save your acceptance. Please try again.", ja: "同意内容を保存できませんでした。もう一度お試しください。" },
  "onboarding.risk.processing": { en: "Processing…", ja: "処理中…" },
  "onboarding.risk.accept": { en: "I Understand and Accept the Risks", ja: "リスクを理解し、同意します" },
  "configure.accountFallback": { en: "Account #{id}", ja: "口座 #{id}" },
  "onboarding.step.create": { en: "Create account", ja: "アカウント作成" },
  "onboarding.step.plan": { en: "Select plan", ja: "プラン選択" },
  "onboarding.step.profile": { en: "Complete profile", ja: "プロフィール設定" },
  "onboarding.step.workspace": { en: "Open workspace", ja: "ワークスペース設定" },
  "onboarding.step.start": { en: "Get started", ja: "利用開始" },
  "register.nameRequired": { en: "First name and last name are required.", ja: "姓と名を入力してください。" },
  "register.alreadyRegistered": { en: "That email address or username is already registered. Sign in or use different details.", ja: "このメールアドレスまたはユーザー名は既に登録されています。ログインするか、別の情報を入力してください。" },
  "register.setup": { en: "Setup", ja: "セットアップ" },
  "register.progress20": { en: "20% complete", ja: "20% 完了" },
  "register.stepOneOfFive": { en: "Step 1 of 5", ja: "全5ステップ中 1" },
  "register.firstName": { en: "First name", ja: "名" },
  "register.lastName": { en: "Last name", ja: "姓" },
};

// =============================================================================
// TRANSLATION FUNCTION
// =============================================================================

/**
 * Get a translated string by key.
 * Falls back to English if key not found, or returns key if neither exists.
 */
export function t(lang: Lang, key: string, values?: Record<string, string | number>): string {
  const entry = dictionary[key];
  if (!entry) {
    console.warn(`[i18n] Missing translation key: ${key}`);
    return key;
  }
  const translated = entry[lang] || entry.en || key;
  if (!values) return translated;
  return translated.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (token, name: string) =>
    Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : token
  );
}

/**
 * Get all dictionary keys (useful for debugging/expansion)
 */
export function getDictionaryKeys(): string[] {
  return Object.keys(dictionary);
}

/** Read-only parity data for contract tests and developer tooling. */
export function getDictionaryEntries(): Readonly<Dictionary> {
  return dictionary;
}
