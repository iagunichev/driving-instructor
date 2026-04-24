"""
Уведомления о бронированиях.

Единственный канал: HTTP POST → PHP-скрипт на хостинге Beget.
Telegram — опционально (если заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID).

Уведомления отправляются ТОЛЬКО при действиях клиента:
  - создание записи   (action='created')
  - перенос записи    (action='rescheduled')
  - отмена записи     (action='cancelled')

При действиях владельца из дашборда — передавать is_client_action=False,
либо просто не вызывать функцию (в views так и сделано).
"""
import logging
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

_ACTION_LABELS = {
    "created":     "📅 Клиент записался",
    "rescheduled": "🔄 Клиент перенёс запись",
    "cancelled":   "❌ Клиент отменил запись",
}


# ── Публичный API ──────────────────────────────────────────────────────────────

def notify_client_action(booking, action: str, is_client_action: bool = True) -> None:
    """
    Уведомляет владельца о действии с записью.

    action: 'created' | 'rescheduled' | 'cancelled'
    is_client_action: False — письмо НЕ отправляется (действие из дашборда).
    """
    if not is_client_action:
        return

    _notify_via_php(booking, action)
    _send_telegram_notification(booking, action)


# ── Вспомогательная функция ────────────────────────────────────────────────────

def _get_booking_info(booking) -> dict:
    """Возвращает дату/время/имя/телефон/комментарий из записи."""
    d = booking.get_display_date
    t = booking.get_display_time or "—"
    return {
        "name":    booking.name,
        "phone":   booking.phone,
        "date":    d.strftime("%d.%m.%Y") if d else "—",
        "time":    t,
        "comment": booking.comment or "",
    }


# ── Канал 1: PHP-скрипт на хостинге ──────────────────────────────────────────

def _notify_via_php(booking, action: str) -> None:
    """Отправляет HTTP POST к send-email.php на хостинге Beget."""
    url = getattr(settings, "MAIL_PHP_URL", "")
    if not url:
        logger.warning("MAIL_PHP_URL не задан — уведомление не отправлено")
        return

    token = getattr(settings, "MAIL_PHP_TOKEN", "")
    info  = _get_booking_info(booking)

    payload = {
        "token":   token,
        "action":  action,
        "name":    info["name"],
        "phone":   info["phone"],
        "date":    info["date"],
        "time":    info["time"],
        "comment": info["comment"],
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req  = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            logger.info("PHP mail (%s) #%s — HTTP %s: %s",
                        action, booking.pk, resp.status, body[:120])
    except Exception as exc:
        logger.error("Ошибка PHP mail (%s) для записи #%s: %s",
                     action, booking.pk, exc)


# ── Канал 2: Telegram (опционально) ──────────────────────────────────────────

def _send_telegram_notification(booking, action: str) -> None:
    """Уведомляет через Telegram Bot (опционально, если задан токен)."""
    token   = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID",   "")
    if not token or not chat_id:
        return

    info  = _get_booking_info(booking)
    label = _ACTION_LABELS.get(action, f"📬 {action}")

    text = (
        f"*{label}*\n\n"
        f"👤 *Имя:*   {info['name']}\n"
        f"📞 *Тел:*   {info['phone']}\n"
        f"📅 *Дата:*  {info['date']}\n"
        f"⏰ *Время:* {info['time']}\n"
    )
    if info["comment"]:
        text += f"\n💬 *Комментарий:* {info['comment']}"

    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5)
        logger.info("Telegram (%s) для записи #%s — OK", action, booking.pk)
    except Exception as exc:
        logger.error("Ошибка Telegram (%s) для записи #%s: %s",
                     action, booking.pk, exc)
