"""Alert channels and manager for Chappie budget/loop/circuit events.

Fires alerts to Slack (via incoming webhook) and/or a generic webhook
endpoint when budget thresholds are crossed or circuit breakers trip.

Design principles:
  - Alerts must never crash the proxy.  Every ``send()`` call catches
    all exceptions and returns ``False`` on failure.
  - HTTP calls use ``httpx.AsyncClient`` with a conservative timeout.
  - The ``AlertManager`` fans out to all configured channels in
    parallel using ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from datetime import datetime, timezone

import httpx

from chappie.config import AlertConfig

logger = logging.getLogger("chappie.alerts")

# Timeout for outbound HTTP calls (connect + read).
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


# ---------------------------------------------------------------------------
# Alert levels
# ---------------------------------------------------------------------------


class AlertLevel(str, enum.Enum):
    """Severity levels tied to budget thresholds and CB trips."""

    INFO = "info"          # 50% budget
    WARNING = "warning"    # 80% budget
    URGENT = "urgent"      # 90% budget
    CRITICAL = "critical"  # 100% budget or circuit breaker trip


_LEVEL_EMOJI: dict[AlertLevel, str] = {
    AlertLevel.INFO: "\u2139\ufe0f",       # info
    AlertLevel.WARNING: "\u26a0\ufe0f",     # warning
    AlertLevel.URGENT: "\U0001f6a8",        # rotating light
    AlertLevel.CRITICAL: "\U0001f6d1",      # stop sign
}


# ---------------------------------------------------------------------------
# Abstract channel
# ---------------------------------------------------------------------------


class AlertChannel:
    """Base class for alert delivery channels."""

    async def send(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        data: dict,
    ) -> bool:
        """Deliver the alert.  Return ``True`` on success, ``False`` on
        failure.  Must never raise."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Slack channel (incoming webhook)
# ---------------------------------------------------------------------------


class SlackChannel(AlertChannel):
    """Send alerts to Slack via an incoming webhook URL.

    Payloads use Slack Block Kit for structured formatting.
    """

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        data: dict,
    ) -> bool:
        emoji = _LEVEL_EMOJI.get(level, "")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Build context line with relevant data fields.
        context_parts = [f"*{level.value.upper()}* | {now}"]
        if data.get("agent_id"):
            context_parts.append(f"Agent: `{data['agent_id']}`")
        if data.get("spent") is not None and data.get("limit") is not None:
            context_parts.append(f"Spend: ${data['spent']:.4f} / ${data['limit']:.4f}")

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} {title}",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": message,
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": " | ".join(context_parts),
                        },
                    ],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(self._webhook_url, json=payload)
                if resp.status_code == 200:
                    logger.debug("Slack alert sent: %s", title)
                    return True
                logger.warning(
                    "Slack webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as exc:
            logger.warning("Slack alert failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Generic webhook channel
# ---------------------------------------------------------------------------


class WebhookChannel(AlertChannel):
    """Send alerts to a generic webhook URL as a JSON POST."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def send(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        data: dict,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()

        payload = {
            "level": level.value,
            "title": title,
            "message": message,
            "data": data,
            "timestamp": now,
        }

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(self._url, json=payload)
                if 200 <= resp.status_code < 300:
                    logger.debug("Webhook alert sent: %s", title)
                    return True
                logger.warning(
                    "Webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception as exc:
            logger.warning("Webhook alert failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Alert manager
# ---------------------------------------------------------------------------


class AlertManager:
    """Fan-out alert dispatcher.

    Initializes channels based on the ``AlertConfig`` and fires alerts
    to all of them in parallel.  Failures are logged but never propagated.
    """

    def __init__(self, config: AlertConfig) -> None:
        self._config = config
        self._channels: list[AlertChannel] = []

        if not config.enabled:
            logger.info("Alerts disabled via config")
            return

        if config.slack_webhook_url:
            self._channels.append(SlackChannel(config.slack_webhook_url))
            logger.info("Alert channel registered: Slack")

        if config.webhook_url:
            self._channels.append(WebhookChannel(config.webhook_url))
            logger.info("Alert channel registered: Webhook")

        if not self._channels:
            logger.info("No alert channels configured")

    @property
    def has_channels(self) -> bool:
        """Return ``True`` if at least one channel is configured."""
        return len(self._channels) > 0

    async def fire(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        """Fire an alert to all configured channels.

        Runs all channel sends concurrently.  Logs failures but never
        raises -- alerts must not disrupt the proxy.
        """
        if not self._channels:
            return

        if not self._config.enabled:
            return

        alert_data = data or {}

        results = await asyncio.gather(
            *(
                channel.send(level, title, message, alert_data)
                for channel in self._channels
            ),
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "Alert channel %d raised unexpectedly: %s",
                    i,
                    result,
                )
            elif result is False:
                logger.warning(
                    "Alert channel %d failed to deliver: level=%s title=%s",
                    i,
                    level.value,
                    title,
                )
