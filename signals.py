"""
Inter-module signal channel — avoids circular imports between bot.py and web.py.

bot.py imports web.py (for admin_app), so web.py cannot import bot.py.
Both import signals.py instead.
"""
import asyncio
import logging

logger = logging.getLogger("eng_assist_bot.signals")

_reconnect: asyncio.Event | None = None


def slack_reconnect_event() -> asyncio.Event:
    """Return the shared asyncio.Event used to signal a Slack reconnect."""
    global _reconnect
    if _reconnect is None:
        _reconnect = asyncio.Event()
    return _reconnect


def trigger_slack_reconnect() -> None:
    """Signal the Slack connection manager to reload tokens and reconnect."""
    slack_reconnect_event().set()
    logger.info("Slack reconnect signal sent")
