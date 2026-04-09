"""LiteLLM custom logger -- the integration point between the proxy and Chappie.

This file wires the loop detector into LiteLLM's ``CustomLogger`` hooks.
Circuit breaker, budget enforcement, and alerting are stubbed for Day 2/3.

Usage in ``litellm_settings`` (proxy config YAML):
    custom_callbacks:
      - chappie.logger

The module-level ``proxy_handler_instance`` is picked up automatically by
the LiteLLM proxy at startup.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from chappie.config import ChappieConfig
from chappie.engine.loop_detector import LoopDetector
from chappie.models import LoopCheckResult

logger = logging.getLogger("chappie.logger")


class ChappieLogger(CustomLogger):
    """LiteLLM proxy hook that enforces loop detection (and, in future
    sprints, circuit breaking, budget caps, and alerting)."""

    def __init__(self) -> None:
        super().__init__()
        self.config = ChappieConfig.from_env()
        self.loop_detector = LoopDetector(self.config.loop_detection)

        # Day 2/3 placeholders -- wired in later sprints.
        self.circuit_breaker = None
        self.budget_enforcer = None
        self.alert_manager = None

        logger.info(
            "Chappie initialised  mode=%s  redis=%s",
            self.config.mode,
            "connected" if self.config.redis_url else "in-memory",
        )

    # ------------------------------------------------------------------
    # Pre-call hook (runs before the LLM request leaves the proxy)
    # ------------------------------------------------------------------

    async def async_pre_call_hook(
        self,
        user_api_key_dict: dict,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> None:
        """Check for loops before allowing the request through."""
        agent_id = self._extract_agent_id(data, user_api_key_dict)
        messages = data.get("messages", [])
        model = data.get("model", "unknown")

        result: LoopCheckResult = self.loop_detector.check(
            agent_id, messages, model,
        )

        if result.is_loop:
            logger.warning(
                "Loop detected  agent=%s  strategy=%s  details=%s  mode=%s",
                agent_id,
                result.strategy,
                result.details,
                self.config.mode,
            )

            if self.config.mode == "enforce":
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "chappie_loop_detected",
                        "agent_id": agent_id,
                        "strategy": result.strategy,
                        "details": result.details,
                        "message": (
                            f"Chappie blocked this request: "
                            f"loop detected via {result.strategy}"
                        ),
                    },
                )
            # In observe mode, just log -- don't block.

    # ------------------------------------------------------------------
    # Success hook (runs after a successful LLM response)
    # ------------------------------------------------------------------

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Record the call in the loop detector and log cost."""
        agent_id = self._extract_agent_id(
            kwargs, kwargs.get("litellm_params", {}).get("metadata", {}),
        )
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", "unknown")

        # Record in loop detector (only successful calls count).
        self.loop_detector.record(agent_id, messages, model)

        # Extract cost if available.
        cost = kwargs.get("response_cost", 0.0)
        if cost:
            logger.info(
                "Call succeeded  agent=%s  model=%s  cost=$%.6f",
                agent_id,
                model,
                cost,
            )

    # ------------------------------------------------------------------
    # Failure hook (placeholder for Day 2 circuit breaker)
    # ------------------------------------------------------------------

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Placeholder -- will feed the circuit breaker in Day 2."""
        agent_id = self._extract_agent_id(
            kwargs, kwargs.get("litellm_params", {}).get("metadata", {}),
        )
        logger.debug(
            "Call failed  agent=%s  model=%s",
            agent_id,
            kwargs.get("model", "unknown"),
        )

    # ------------------------------------------------------------------
    # Agent ID extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_agent_id(
        kwargs: dict,
        user_api_key_dict: dict | Any,
    ) -> str:
        """Derive a stable agent identifier using a fallback chain:

        1. metadata.agent_id   (explicit, preferred)
        2. metadata.session_id (common in LangChain / AutoGen)
        3. team:user           (proxy-level identity)
        4. api_key suffix      (last resort)

        Returns ``"unknown"`` if nothing is available.
        """
        # Pull metadata from the most common locations.
        metadata: dict = {}
        if isinstance(kwargs, dict):
            metadata = kwargs.get("metadata", {})
            if not metadata:
                litellm_params = kwargs.get("litellm_params", {})
                if isinstance(litellm_params, dict):
                    metadata = litellm_params.get("metadata", {})

        if isinstance(metadata, dict):
            if metadata.get("agent_id"):
                return str(metadata["agent_id"])
            if metadata.get("session_id"):
                return str(metadata["session_id"])

        # Fallback: team + user from api key dict.
        api_dict = (
            user_api_key_dict
            if isinstance(user_api_key_dict, dict)
            else {}
        )
        team = api_dict.get("team_id", "")
        user = api_dict.get("user_id", "")
        if team or user:
            return f"{team or 'no_team'}:{user or 'no_user'}"

        # Last resort: tail of the hashed API key.
        api_key = api_dict.get("api_key", "")
        if api_key:
            return f"key_{api_key[-8:]}"

        return "unknown"


# Module-level instance picked up by LiteLLM proxy.
proxy_handler_instance = ChappieLogger()
