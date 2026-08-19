from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import CustomerNotification


def _line(value, fallback: str = "") -> str:
    text = str(value if value not in (None, "") else fallback)
    return " ".join(text.replace("\x00", "").split())[:160]


def _side(value, lang: str) -> str:
    key = str(value or "").upper()
    if lang == "ja":
        return {"BUY": "買い", "SELL": "売り"}.get(key, _line(value))
    return {"BUY": "Buy", "SELL": "Sell"}.get(key, _line(value))


def _account(payload: dict, lang: str) -> str:
    kind = payload.get("account_kind", "demo")
    label = ("デモ口座" if kind == "demo" else "取引口座") if lang == "ja" else (
        "Demo account" if kind == "demo" else "Trading account")
    number = _line(payload.get("account_number"))
    return f"{label} · {number}" if number else label


def _money(value, currency: str) -> str:
    try:
        amount = Decimal(str(value))
        sign = "+" if amount > 0 else ""
        return f"{sign}{amount:.2f} {currency or 'USD'}"
    except (InvalidOperation, TypeError, ValueError):
        return ""


def _timestamp(value) -> str:
    text = _line(value)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _outcome(value, lang: str) -> str:
    key = str(value or "").upper()
    if lang == "ja":
        return {"WIN": "利益", "LOSS": "損失", "BREAKEVEN": "損益なし"}.get(key, "")
    return {"WIN": "Win", "LOSS": "Loss", "BREAKEVEN": "Breakeven"}.get(key, "")


def render_customer_message(notification: CustomerNotification) -> str:
    """Render the allow-listed EN/JA catalogue. No raw diagnostic field is ever consulted."""
    lang = notification.language if notification.language in ("en", "ja") else "en"
    p = notification.payload if isinstance(notification.payload, dict) else {}
    event = notification.event_type
    strategy = _line(p.get("strategy"), "GuvFX")
    symbol = _line(p.get("symbol"))
    side = _side(p.get("side"), lang)

    if event == CustomerNotification.EventType.CONNECTION_CONFIRMED:
        if lang == "ja":
            return "Telegram通知を接続しました\n\nGuvFXの取引更新をこのチャットで受け取れます。通知設定はGuvFXでいつでも変更できます。"
        return "Telegram notifications connected\n\nYou’ll receive GuvFX trading updates in this chat. You can change your notification settings in GuvFX at any time."

    if event == CustomerNotification.EventType.TRADE_OPENED:
        rows = [
            "GuvFXの取引が開始されました" if lang == "ja" else "GuvFX trade opened",
            "", strategy, f"{symbol} · {side}",
        ]
        volume = _line(p.get("volume"))
        if volume:
            rows.append(f"取引量: {volume}ロット" if lang == "ja" else f"{volume} lots")
        labels = (
            (("エントリー", "entry"), ("ストップロス", "stop_loss"), ("テイクプロフィット", "take_profit"))
            if lang == "ja" else
            (("Entry", "entry"), ("Stop loss", "stop_loss"), ("Take profit", "take_profit"))
        )
        rows.extend(f"{label}: {_line(p.get(key))}" for label, key in labels if p.get(key) not in (None, ""))
        occurred_at = _timestamp(p.get("occurred_at"))
        if occurred_at:
            rows.append(("時刻: " if lang == "ja" else "Time: ") + occurred_at)
        rows.extend(["", _account(p, lang)])
        return "\n".join(rows)

    if event in (
        CustomerNotification.EventType.TRADE_UPDATED,
        CustomerNotification.EventType.TRADE_CLOSED,
    ):
        is_update = event == CustomerNotification.EventType.TRADE_UPDATED
        result = _money(p.get("result"), _line(p.get("currency"), "USD"))
        rows = [
            (
                "GuvFXの取引状況" if is_update else "GuvFXの取引が終了しました"
            ) if lang == "ja" else (
                "GuvFX trade update" if is_update else "GuvFX trade closed"
            ),
            "", strategy, f"{symbol} · {side}",
        ]
        progress_label = _line(p.get("progress_label"))
        if is_update and progress_label:
            rows.append(f"{progress_label} 到達" if lang == "ja" else f"{progress_label} reached")
        if result:
            result_label = (
                "現在の確定損益: " if is_update else "最終損益: "
            ) if lang == "ja" else (
                "Realised so far: " if is_update else "Final result: "
            )
            rows.append(result_label + result)
        outcome = _outcome(p.get("outcome"), lang)
        if outcome:
            rows.append(("結果: " if lang == "ja" else "Outcome: ") + outcome)
        closed = p.get("progress_closed")
        total = p.get("progress_total")
        if isinstance(closed, int) and isinstance(total, int) and total > 0:
            rows.append(
                f"{total}ポジション中{closed}ポジションを決済"
                if lang == "ja" else f"{closed} of {total} trade legs closed"
            )
        occurred_at = _timestamp(p.get("occurred_at"))
        if occurred_at:
            rows.append(("時刻: " if lang == "ja" else "Time: ") + occurred_at)
        rows.extend(["", _account(p, lang)])
        return "\n".join(rows)

    if event in (CustomerNotification.EventType.STRATEGY_ENABLED,
                 CustomerNotification.EventType.STRATEGY_DISABLED):
        enabled = event == CustomerNotification.EventType.STRATEGY_ENABLED
        if lang == "ja":
            title = "ストラテジーを有効にしました" if enabled else "ストラテジーを無効にしました"
        else:
            title = "Strategy enabled" if enabled else "Strategy disabled"
        return "\n".join([title, "", strategy, _account(p, lang)])

    if event == CustomerNotification.EventType.EXECUTION_PROBLEM:
        code = p.get("message_code")
        if lang == "ja":
            body = {
                "workspace_attention": "取引ワークスペースを確認してください。",
                "trade_not_placed": "Wayondの取引を発注できませんでした。",
            }.get(code, "現在、取引を一時的に利用できません。")
            return f"GuvFXからのお知らせ\n\n{body}\n問題が続く場合はサポートにお問い合わせください。"
        body = {
            "workspace_attention": "Your trading workspace needs attention.",
            "trade_not_placed": "We couldn’t place a Wayond trade.",
        }.get(code, "Trading is temporarily unavailable.")
        return f"GuvFX needs your attention\n\n{body}\nContact support if the problem continues."

    if event == CustomerNotification.EventType.WORKSPACE_READY:
        if lang == "ja":
            return f"取引ワークスペースの準備が完了しました\n\n{_account(p, lang)}\nGuvFXで次の手順を確認できます。"
        return f"Your trading workspace is ready\n\n{_account(p, lang)}\nYou can review the next step in GuvFX."

    raise ValueError("unsupported_customer_notification_event")
