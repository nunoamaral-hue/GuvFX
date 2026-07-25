//+------------------------------------------------------------------+
//|  GuvfxProbeEA.mq5                                                 |
//|  GuvFX B3P-2 disposable-demo DIAGNOSTIC probe.                    |
//|                                                                   |
//|  NON-TRADING BY CONSTRUCTION. This EA records terminal / account /|
//|  connectivity / market-data / chart facts to a slot-contained log |
//|  so the platform can judge Session-0 automated-trading viability. |
//|  It places NO order of any kind and reaches no trade API. It is a |
//|  validation test artefact, not a production strategy.             |
//|                                                                   |
//|  Safety invariants (asserted by tests_diagnostic_ea.py):          |
//|   - no OrderSend / OrderSendAsync / CTrade buy-sell / pending;    |
//|   - no external network (no WebRequest / Socket*);                |
//|   - no DLL import (#import);                                       |
//|   - no credential logging (ACCOUNT_LOGIN value / password never   |
//|     printed - only a login_present boolean and the shared server  |
//|     name are recorded);                                           |
//|   - writes ONLY inside this terminal's MQL5\Files (the slot) -    |
//|     never FILE_COMMON;                                            |
//|   - fails closed if the account classifies as anything but DEMO.  |
//+------------------------------------------------------------------+
#property copyright "GuvFX B3P-2 validation"
#property version   "1.00"
#property description "GuvFX diagnostic probe EA - NON-TRADING. Records terminal/account/market-data state to a slot-contained log. Places no orders."

#define GUVFX_PROBE_ID  "GUVFX-PROBE-EA-B3P2"
#define GUVFX_LOG_FILE  "guvfx_probe.log"

input int    GuvfxTimerSeconds = 5;    // heartbeat cadence (seconds)
input string GuvfxSymbol       = "";   // symbol to watch; empty = the chart symbol

long     g_timer_count = 0;
long     g_tick_count  = 0;
datetime g_first_tick  = 0;
bool     g_fatal       = false;        // fail-closed latch (non-demo account)
string   g_symbol      = "";

//+------------------------------------------------------------------+
string GuvfxAccountClass()
  {
   if(AccountInfoInteger(ACCOUNT_LOGIN) == 0) return("none");    // account-free trial: no login yet
   long mode = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_DEMO)    return("demo");
   if(mode == ACCOUNT_TRADE_MODE_CONTEST) return("contest");
   if(mode == ACCOUNT_TRADE_MODE_REAL)    return("REAL");
   return("unknown");
  }

//+------------------------------------------------------------------+
//| Slot-contained append log. NO FILE_COMMON: writes to THIS         |
//| terminal's MQL5\Files, i.e. the runtime slot directory.           |
//+------------------------------------------------------------------+
void GuvfxLog(const string tag, const string msg)
  {
   int h = FileOpen(GUVFX_LOG_FILE, FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      Print(GUVFX_PROBE_ID, " logfile open failed err=", GetLastError());
      return;
     }
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, StringFormat("%s [%s] %s", TimeToString(TimeLocal(), TIME_DATE|TIME_SECONDS), tag, msg));
   FileClose(h);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_symbol = (StringLen(GuvfxSymbol) > 0) ? GuvfxSymbol : _Symbol;

   // Fail closed on a non-demo account. login==0 (no account yet) is NOT a failure - the account-free runtime
   // trial runs before any login. A REAL account IS a hard stop: refuse to operate.
   if(AccountInfoInteger(ACCOUNT_LOGIN) != 0 && AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
     {
      g_fatal = true;
      GuvfxLog("FATAL", "account is not DEMO (class=" + GuvfxAccountClass() + ") - refusing to operate");
      return(INIT_FAILED);
     }

   SymbolSelect(g_symbol, true);
   GuvfxLog("INIT", StringFormat("id=%s build=%d data_path=%s symbol=%s account_class=%s login_present=%s",
            GUVFX_PROBE_ID, (int)TerminalInfoInteger(TERMINAL_BUILD),
            TerminalInfoString(TERMINAL_DATA_PATH), g_symbol, GuvfxAccountClass(),
            (AccountInfoInteger(ACCOUNT_LOGIN) != 0) ? "true" : "false"));
   EventSetTimer(MathMax(1, GuvfxTimerSeconds));
   GuvfxLog("INIT_RESULT", "INIT_SUCCEEDED");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   GuvfxLog("DEINIT", StringFormat("reason=%d timer_count=%d tick_count=%d",
            reason, (int)g_timer_count, (int)g_tick_count));
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   g_tick_count++;
   if(g_first_tick == 0) g_first_tick = TimeCurrent();
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   if(g_fatal) return;                                    // failed closed: emit nothing further
   g_timer_count++;

   double bid        = SymbolInfoDouble(g_symbol, SYMBOL_BID);
   double ask        = SymbolInfoDouble(g_symbol, SYMBOL_ASK);
   long   trade_mode = SymbolInfoInteger(g_symbol, SYMBOL_TRADE_MODE);   // FULL/CLOSEONLY/DISABLED (open proxy)
   bool   connected  = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool   selected   = (bool)SymbolInfoInteger(g_symbol, SYMBOL_SELECT);
   bool   term_trade = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED); // AutoTrading (terminal permission)
   bool   ea_trade   = (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);           // EA-level permission
   long   chart_id   = ChartID();
   long   chart_wins = ChartGetInteger(0, CHART_WINDOWS_TOTAL);
   int    err        = GetLastError();

   GuvfxLog("HB", StringFormat(
      "t=%s timer=%d ticks=%d first_tick=%s connected=%s account=%s login_present=%s server=%s symbol=%s "
      + "selected=%s bid=%s ask=%s sym_trade_mode=%d term_trade_allowed=%s ea_trade_allowed=%s "
      + "chart_id=%d chart_windows=%d last_err=%d",
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS), (int)g_timer_count, (int)g_tick_count,
      (g_first_tick > 0) ? TimeToString(g_first_tick, TIME_DATE|TIME_SECONDS) : "none",
      connected ? "true" : "false", GuvfxAccountClass(),
      (AccountInfoInteger(ACCOUNT_LOGIN) != 0) ? "true" : "false",
      AccountInfoString(ACCOUNT_SERVER), g_symbol, selected ? "true" : "false",
      DoubleToString(bid, _Digits), DoubleToString(ask, _Digits), (int)trade_mode,
      term_trade ? "true" : "false", ea_trade ? "true" : "false",
      (int)chart_id, (int)chart_wins, err));
  }
//+------------------------------------------------------------------+
