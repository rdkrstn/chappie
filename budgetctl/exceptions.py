"""Custom exception hierarchy for BudgetCtl.

All exceptions inherit from ``BudgetCtlError`` so callers can catch the
entire family with a single ``except BudgetCtlError`` clause.
"""

from __future__ import annotations


class BudgetCtlError(Exception):
    """Base exception for all BudgetCtl errors."""


class BudgetCtlLoopDetected(BudgetCtlError):
    """Raised when the loop detector identifies a runaway agent."""

    def __init__(
        self,
        agent_id: str,
        strategy: str,
        details: str,
    ) -> None:
        self.agent_id = agent_id
        self.strategy = strategy
        self.details = details
        super().__init__(
            f"Loop detected for agent {agent_id} "
            f"[strategy={strategy}]: {details}"
        )


class BudgetCtlBudgetExceeded(BudgetCtlError):
    """Raised when an agent has exhausted its spend budget."""

    def __init__(
        self,
        agent_id: str,
        spent: float,
        limit: float,
    ) -> None:
        self.agent_id = agent_id
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"Budget exceeded for agent {agent_id}: "
            f"spent ${spent:.4f} / limit ${limit:.4f}"
        )


class BudgetCtlCircuitOpen(BudgetCtlError):
    """Raised when the circuit breaker is open and the call is rejected."""

    def __init__(
        self,
        agent_id: str,
        reason: str,
    ) -> None:
        self.agent_id = agent_id
        self.reason = reason
        super().__init__(
            f"Circuit open for agent {agent_id}: {reason}"
        )
