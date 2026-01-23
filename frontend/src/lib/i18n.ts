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

  // -----------------------------------------------------------------------------
  // Accounts - Page Header
  // -----------------------------------------------------------------------------
  "accounts.title": { en: "Trading Accounts", ja: "取引口座" },
  "accounts.subtitle": {
    en: "Link your broker / MT5 accounts so GuvFX can map strategies and trades.",
    ja: "ブローカー/MT5口座を連携し、GuvFXで戦略と取引を紐付けます。",
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
    en: "This is the password for your broker's trading platform account (e.g. MetaTrader 5). It will be stored securely and used later to connect to your account.",
    ja: "ブローカーの取引プラットフォーム（例: MetaTrader 5）のパスワードです。安全に保存され、口座への接続に使用されます。",
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
  "accounts.accountAdded": { en: "✅ Account added / MT5 login successful.", ja: "✅ 口座追加 / MT5ログイン成功" },
  "accounts.testSuccess": { en: "✅ MT5 session matches this account (EA validation OK).", ja: "✅ MT5セッションがこの口座と一致（EA検証OK）" },
  "accounts.testFailed": { en: "❌ Not matched:", ja: "❌ 不一致:" },
  "accounts.setActive": { en: "Account set to ACTIVE.", ja: "口座を有効に設定しました。" },
  "accounts.setInactive": { en: "Account set to INACTIVE.", ja: "口座を無効に設定しました。" },
  "accounts.failedActiveStatus": { en: "Failed to change active status", ja: "有効/無効の切り替えに失敗しました" },

  // -----------------------------------------------------------------------------
  // Login - Page Header
  // -----------------------------------------------------------------------------
  "login.welcomeBack": { en: "Welcome back to", ja: "おかえりなさい" },
  "login.subtitle": {
    en: "Log in to manage strategies, review backtests, and get AI-powered guidance on your trading.",
    ja: "ログインして戦略の管理、バックテストの確認、AIによる取引ガイダンスを利用しましょう。",
  },
  "login.logIn": { en: "Log in", ja: "ログイン" },
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
  "landing.getStarted": { en: "Get Started", ja: "始める" },

  // -----------------------------------------------------------------------------
  // Landing Page - Hero Section
  // -----------------------------------------------------------------------------
  "landing.heroTitle": { en: "Automated Trading Intelligence", ja: "自動トレーディングインテリジェンス" },
  "landing.heroSubtitle": {
    en: "Design algorithmic strategies, run backtests, and deploy with AI-powered analysis. Built for serious traders.",
    ja: "アルゴリズム戦略の設計、バックテストの実行、AIによる分析を活用した展開。本格的なトレーダーのために構築。",
  },
  "landing.heroCTA": { en: "Start Building", ja: "戦略を始める" },
  "landing.heroSecondaryCTA": { en: "Learn More", ja: "詳細を見る" },

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
  // Register Page
  // -----------------------------------------------------------------------------
  "register.welcomeTo": { en: "Welcome to", ja: "ようこそ" },
  "register.subtitle": {
    en: "Start automating your trading with ease. Design strategies, run backtests, and get AI-powered insights.",
    ja: "取引の自動化を簡単に始めましょう。戦略の設計、バックテストの実行、AIによる洞察を取得。",
  },
  "register.getStarted": { en: "Get started", ja: "始める" },
  "register.login": { en: "Log in", ja: "ログイン" },
  "register.signUp": { en: "Sign up", ja: "新規登録" },
  "register.createAccount": { en: "Create an Account", ja: "アカウント作成" },
  "register.step": { en: "Step 1 of 12", ja: "ステップ 1/12" },
  "register.email": { en: "Email", ja: "メールアドレス" },
  "register.emailPlaceholder": { en: "Email", ja: "メールアドレス" },
  "register.password": { en: "Password", ja: "パスワード" },
  "register.passwordPlaceholder": { en: "Must be at least 3 characters", ja: "3文字以上" },
  "register.username": { en: "Username (optional)", ja: "ユーザー名（任意）" },
  "register.usernamePlaceholder": { en: "Defaults to your email if left empty", ja: "空欄の場合はメールアドレスが使用されます" },
  "register.continue": { en: "Continue", ja: "続行" },
  "register.creating": { en: "Creating account...", ja: "アカウント作成中..." },
  "register.passwordTooShort": { en: "Password must be at least 3 characters.", ja: "パスワードは3文字以上必要です。" },
  "register.success": { en: "Account created for {email}. You can now log in.", ja: "{email}のアカウントが作成されました。ログインできます。" },
  "register.errorDefault": { en: "Registration failed.", ja: "登録に失敗しました。" },
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
