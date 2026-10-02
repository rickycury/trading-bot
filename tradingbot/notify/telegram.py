"""Telegram notifier.

Implements the `Notifier` interface from core.execution, so it drops straight
into AlertOnly (or any backend) as the thing that actually reaches your phone.
A dead `ConsoleNotifier` line in a log file you're not watching is not an alert;
a Telegram push is.

Setup (two minutes):
  1. DM @BotFather on Telegram -> /newbot -> copy the bot token.
  2. DM your new bot anything, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and read your chat id out of
     the JSON (result[].message.chat.id).
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment, or pass them
     to the constructor.

Design notes:
  * Network failures never propagate. If Telegram is down or misconfigured the
    notifier logs the problem and returns — a flaky messenger must not take the
    trading loop down with it.
  * `requests` is an optional dependency; without it the notifier degrades to a
    log line instead of crashing on import.
  * This is one-way (alerts out). The SemiAuto approve/reject flow needs inline
    keyboards plus polling getUpdates for callback queries — see the note at the
    bottom for where that hooks in.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Optional

from ..core.execution import Notifier

log = logging.getLogger("bot")

try:
    import requests
    _REQUESTS_OK = True
except ImportError:  # pragma: no cover
    _REQUESTS_OK = False


class TelegramNotifier(Notifier):
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout = timeout
        self._warned = False

        if not _REQUESTS_OK:
            log.warning("TelegramNotifier: `requests` not installed; alerts will only be logged.")
        elif not (self.bot_token and self.chat_id):
            log.warning("TelegramNotifier: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset; "
                        "alerts will only be logged.")

    @property
    def enabled(self) -> bool:
        return bool(_REQUESTS_OK and self.bot_token and self.chat_id)

    def send(self, title: str, body: str) -> None:
        # Always log, so there's a local record even when Telegram is reachable.
        log.warning("ALERT [%s] %s", title, body)
        if not self.enabled:
            return

        text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=self.timeout,
            )
            if resp.status_code != 200 and not self._warned:
                log.error("TelegramNotifier: send failed (%s): %s", resp.status_code, resp.text[:200])
                self._warned = True
        except Exception as e:  # network blip must not break the trading loop
            if not self._warned:
                log.error("TelegramNotifier: send error: %s", e)
                self._warned = True

    def self_test(self) -> bool:
        """Send a probe message. Returns True if Telegram accepted it. Handy in a
        startup check so a broken token surfaces immediately, not on the first
        real alert."""
        if not self.enabled:
            log.error("TelegramNotifier.self_test: not configured (token/chat id/requests missing).")
            return False
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                params={"chat_id": self.chat_id, "text": "tradingbot: notifier online ✅"},
                timeout=self.timeout,
            )
            ok = resp.status_code == 200
            if not ok:
                log.error("TelegramNotifier.self_test failed (%s): %s", resp.status_code, resp.text[:200])
            return ok
        except Exception as e:
            log.error("TelegramNotifier.self_test error: %s", e)
            return False


# To graduate to SemiAuto (approve/reject from the chat):
#   * send() attaches reply_markup with inline_keyboard buttons whose
#     callback_data encodes the pending order id;
#   * a small poller hits getUpdates (or a webhook) for callback_query events and
#     resolves the corresponding order. That belongs in a SemiAuto(Execution)
#     backend that blocks on confirmation before calling submit — not in the
#     notifier itself, which stays one-way.
