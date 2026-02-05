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
  "ui.home": { en: "Home", ja: "ホーム" },
  "ui.languageLabel": { en: "Language", ja: "言語" },
  "ui.english": { en: "English", ja: "英語" },
  "ui.japanese": { en: "Japanese", ja: "日本語" },
  "ui.langEnglish": { en: "English", ja: "English" },
  "ui.langJapanese": { en: "日本語", ja: "日本語" },

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
  "dashboard.trustMiniBody": {
    en: "You control all trading decisions. This platform does not execute trades without your action.",
    ja: "すべての取引判断はあなたが行います。操作なしに取引が実行されることはありません。",
  },

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
  "landing.ctaPrimary": { en: "Get Started (Free)", ja: "無料で始める" },
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
    en: "Start automating your trading with ease. Design strategies, run backtests, and get AI-powered insights.",
    ja: "取引の自動化を簡単に始めましょう。戦略の設計、バックテストの実行、AIによる洞察を取得。",
  },
  "register.getStarted": { en: "Get started", ja: "始める" },
  "register.login": { en: "Log in", ja: "ログイン" },
  "register.signUp": { en: "Sign up", ja: "新規登録" },
  "register.createAccount": { en: "Create an Account", ja: "アカウント作成" },
  "register.stepIndicator": { en: "Step 1 of 5", ja: "ステップ 1/5" },
  "register.stepTitle": { en: "Account Creation", ja: "アカウント作成" },
  "register.stepNote": {
    en: "You can complete security, hosting, and verification later. For now, create your account.",
    ja: "セキュリティ、ホスティング、認証は後で設定できます。まずはアカウントを作成してください。",
  },
  "register.nextTitle": { en: "Coming next", ja: "次に設定できる項目" },
  "register.nextEmailVerify": { en: "Email verification", ja: "メール認証" },
  "register.nextHosting": { en: "Hosting selection", ja: "ホスティング選択" },
  "register.nextProfile": { en: "Profile & compliance", ja: "プロフィール・コンプライアンス" },
  "register.nextSecurity": { en: "Security (2FA)", ja: "セキュリティ（2FA）" },
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
  "register.trustMiniTitle": { en: "Your control, your decisions", ja: "あなたの主導、あなたの判断" },
  "register.trustMiniBody": {
    en: "GuvFX provides strategy tools only. No trades execute without your explicit action. You remain in control.",
    ja: "GuvFXは戦略ツールのみを提供します。明示的な操作なしに取引は実行されません。主導権は常にあなたにあります。",
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
  "marketplace.filterStructure": {
    en: "Structure",
    ja: "ストラクチャー",
  },
  "marketplace.filterPatterns": {
    en: "Patterns",
    ja: "パターン",
  },
  "marketplace.selectAccount": {
    en: "Select account",
    ja: "口座を選択",
  },
  "marketplace.assign": {
    en: "Assign",
    ja: "割当",
  },
  "marketplace.assigning": {
    en: "Assigning\u2026",
    ja: "割当中\u2026",
  },
  "marketplace.preview": {
    en: "Preview",
    ja: "プレビュー",
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
    en: "Assign endpoint not found. The server may not yet support this feature.",
    ja: "割当エンドポイントが見つかりません。サーバーがこの機能に未対応の可能性があります。",
  },
  "marketplace.alertUnexpectedResponse": {
    en: "Assignment failed (unexpected server response). Please refresh and try again.",
    ja: "割り当てに失敗しました（予期しないサーバー応答）。ページを更新して再度お試しください。",
  },
  "marketplace.alertAssignFailed": {
    en: "Assignment failed.",
    ja: "割り当てに失敗しました。",
  },
  "marketplace.alertPreviewSoon": {
    en: "Preview coming soon.",
    ja: "プレビューは近日公開予定です。",
  },
  "marketplace.emptyTitle": {
    en: "No templates match your filters.",
    ja: "フィルターに一致するテンプレートがありません。",
  },
  "marketplace.emptyHint": {
    en: "Try adjusting your search or category filter.",
    ja: "検索条件やカテゴリーフィルターを変更してみてください。",
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
