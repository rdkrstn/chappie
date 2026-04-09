"""LiteLLM custom logger -- the integration point between the proxy and Chappie.

This file wires the loop detector and circuit breaker into LiteLLM's
``CustomLogger`` hooks.  Budget enforcement and alerting are stubbed
for Day 3.

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
from chappie.engine.circuit_breaker import CircuitBreaker, TripReason
from chappie.engine.loop_detector import LoopDetector
from chappie.models import CircuitBreakerState, LoopCheckResult
from chappie.store.memory import MemoryStore

logger = logging.getLogger("chappie.logger")


def _build_store(config: ChappieConfig) -> Any:
    """Create the backing store based on configuration.

    If ``config.redis_url`` is set, attempt to instantiate a RedisStore.
    On any import or connection-init error, fall back to MemoryStore so
    the proxy always starts.
    """
    if config.redis_url:
        try:
            from chappie.store.redis import RedisStore

            store = RedisStore(config.redis_url)
            logger.info("Store: RedisStore  url=%s", config.redis_url)
            return store
        except Exception as exc:
            logger.warning(
                "RedisStore init failed, falling back to MemoryStore: %s",
                exc,
            )

    store = MemoryStore()
    logger.info("Store: MemoryStore (in-memory)")
    return store


class ChappieLogger(CustomLogger):
    """LiteLLM proxy hook that enforces loop detection and circuit
    breaking.  Budget caps and alerting are wired in Day 3."""

    def __init__(self) -> None:
        super().__init__()
        self.config = ChappieConfig.from_env()
        self.store = _build_store(self.config)
        self.loop_detector = LoopDetector(self.config.loop_detection)
        self.circuit_breaker = CircuitBreaker(
            self.store, self.config.circuit_breaker,
        )

        # Day 3 placeholders -- wired in later sprints.
        self.budget_enforcer = None
        self.alert_manager = None

        logger.info(
            "Chappie initialised  mode=%s  store=%s",
            self.config.mode,
            type(self.store).__name__,
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
        """Check circuit breaker state and loop detection before allowing
        the request through.

        Order of checks:
          1. Circuit breaker (fast -- stored state lookup)
          2. Loop detection  (runs three strategies against the window)

        If any check fails in enforce mode, an HTTP 429 is raised.
        In observe mode, warnings are logged but the request proceeds.
        """
        agent_id = self._extract_agent_id(data, user_api_key_dict)
        messages = data.get("messages", [])
        model = data.get("model", "unknown")

        # --- Step 1: Circuit breaker check ---
        cb_info = await self.circuit_breaker.check(agent_id)

        if cb_info.state == CircuitBreakerState.OPEN:
            logger.warning(
                "Circuit breaker OPEN  agent=%s  reason=%s  open_until=%s  mode=%s",
                agent_id,
                cb_info.reason,
                cb_info.open_until,
                self.config.mode,
            )
            if self.config.mode == "enforce":
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "chappie_circuit_open",
                        "agent_id": agent_id,
                        "reason": cb_info.reason,
                        "open_until": (
                            cb_info.open_until.isoformat()
                            if cb_info.open_until
                            else None
                        ),
                        "message": (
                            f"Chappie blocked this request: "
                            f"circuit breaker is open ({cb_info.reason})"
                        ),
                    },
                )
            # Observe mode: log but allow through.

        elif cb_info.state == CircuitBreakerState.HALF_OPEN:
            logger.info(
                "Circuit breaker HALF_OPEN  agent=%s  allowing probe request",
                agent_id,
            )

        # --- Step 2: Loop detection ---
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
                # Trip the circuit breaker so subsequent calls are blocked
                # without needing to re-run loop detection each time.
                await self.circuit_breaker.trip(
                    agent_id,
                    TripReason.LOOP_DETECTED,
                    details=f"Loop detected via {result.strategy}: {result.details}",
                )

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
            # In observe mode: log the detection but do not trip the CB.

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
        """Record the call in the loop detector, notify the circuit
        breaker of a successful response, and log cost."""
        agent_id = self._extract_agent_id(
            kwargs, kwargs.get("litellm_params", {}).get("metadata", {}),
        )
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", "unknown")

        # Record in loop detector (only successful calls count).
        self.loop_detector.record(agent_id, messages, model)

        # Notify circuit breaker -- handles HALF_OPEN -> CLOSED transition.
        await self.circuit_breaker.record_success(agent_id)

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
    # Failure hook (feeds the circuit breaker error counter)
    # ------------------------------------------------------------------

    async def async_log_failure_event(
        self,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Record a failure in the circuit breaker.  If the error count
        crosses the threshold, the CB trips automatically.

        Also releases any budget reservation (placeholder for Day 3).
        """
        agent_id = self._extract_agent_id(
            kwargs, kwargs.get("litellm_params", {}).get("metadata", {}),
        )
        model = kwargs.get("model", "unknown")

        logger.warning(
            "Call failed  agent=%s  model=%s",
            agent_id,
            model,
        )

        # Record failure -- may trip the circuit breaker if the error
        # threshold is exceeded within the configured window.
        await self.circuit_breaker.record_failure(agent_id)

        # Day 3: Release any budget reservation held for this call.
        # if self.budget_enforcer:
        #     await self.budget_enforcer.release(agent_id, reservation_id)

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
