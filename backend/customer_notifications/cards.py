from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import CustomerNotification


def _text(value, limit=80) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def customer_result_card_model(notification: CustomerNotification) -> dict:
    """Customer-only card data. No stakeholder contract or arbitrary payload is consulted."""
    if notification.event_type != CustomerNotification.EventType.TRADE_CLOSED:
        raise ValueError("customer_result_card_requires_final_outcome")
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    outcome = _text(payload.get("outcome")).upper()
    if outcome not in {"WIN", "LOSS", "BREAKEVEN"}:
        raise ValueError("customer_result_card_outcome_invalid")
    result = _text(payload.get("result"), 32)
    currency = _text(payload.get("currency") or "USD", 8)
    if not result:
        raise ValueError("customer_result_card_result_missing")
    lang = notification.language if notification.language in {"en", "ja"} else "en"
    labels = {
        "en": {
            "heading": "COMPLETED TRADE RESULT",
            "result": "Realised result",
            "completed": "Completed positions",
            "account": "MT5 account",
            "footer": "Customer result · Generated from durable GuvFX records",
            "outcomes": {"WIN": "WIN", "LOSS": "LOSS", "BREAKEVEN": "BREAKEVEN"},
        },
        "ja": {
            "heading": "確定した取引結果",
            "result": "確定損益",
            "completed": "決済ポジション",
            "account": "MT5口座",
            "footer": "お客様の確定結果 · GuvFXの記録から生成",
            "outcomes": {"WIN": "利益", "LOSS": "損失", "BREAKEVEN": "損益なし"},
        },
    }[lang]
    return {
        "brand": "GuvFX",
        "strategy": _text(payload.get("strategy") or "Wayond WIM Strategy"),
        "symbol": _text(payload.get("symbol"), 24),
        "outcome": outcome,
        "result": f"{result} {currency}",
        "progress": f"{int(payload.get('progress_closed'))} / {int(payload.get('progress_total'))}",
        "account": _text(payload.get("account_number"), 32),
        "heading": labels["heading"],
        "result_label": labels["result"],
        "completed_label": labels["completed"],
        "account_label": labels["account"],
        "footer": labels["footer"],
        "outcome_display": labels["outcomes"][outcome],
    }


def _font(size: int, bold: bool = False):
    candidates = [
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        # Installed by backend/Dockerfile. Keep this ahead of Latin-only fallbacks so
        # Japanese result cards render deterministically in the production container.
        Path(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
            if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        ),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def render_customer_result_card(notification: CustomerNotification) -> bytes:
    """Render a branded customer result card using only the safe model above."""
    model = customer_result_card_model(notification)
    outcome = model["outcome"]
    accent = "#43d17a" if outcome == "WIN" else "#ff6b72" if outcome == "LOSS" else "#9aa9c1"
    image = Image.new("RGB", (1080, 1080), "#060b1a")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 55, 1025, 1025), radius=42, fill="#0b1530", outline="#20365d", width=3)
    draw.rounded_rectangle((55, 55, 1025, 225), radius=42, fill="#10264a")
    draw.rectangle((55, 180, 1025, 225), fill="#10264a")
    draw.text((105, 100), "GuvFX", font=_font(66, True), fill="#ffffff")
    draw.text((105, 180), model["strategy"], font=_font(30), fill="#a8c7ef")
    draw.text((105, 310), model["heading"], font=_font(30, True), fill="#8fa5c6")
    draw.text((105, 375), model["outcome_display"], font=_font(98, True), fill=accent)
    draw.text((105, 535), model["symbol"], font=_font(48, True), fill="#f2f7ff")
    draw.text((105, 620), model["result_label"], font=_font(28), fill="#8fa5c6")
    draw.text((105, 660), model["result"], font=_font(62, True), fill=accent)
    draw.text((105, 790), f"{model['completed_label']}  {model['progress']}", font=_font(30), fill="#d7e5f7")
    if model["account"]:
        draw.text((105, 850), f"{model['account_label']}  {model['account']}", font=_font(27), fill="#8fa5c6")
    draw.text((105, 945), model["footer"], font=_font(22), fill="#637895")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
