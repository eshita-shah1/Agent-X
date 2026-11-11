"""Whether simulated execution is available on a date, and why not (served by GET /exchange/status).

One function, so the status endpoint and the Phase 5 agent (which runs SIGNALS_ONLY when execution is unavailable,
decision D9) always report the same blocking reasons.
"""

from dataclasses import dataclass
from datetime import date

from app.core.config import Settings
from app.exchange.costs import CostModel, CostScheduleError, load_cost_schedule


@dataclass(frozen=True)
class ExecutionReadiness:
    blocking_reasons: list[str]
    costs: CostModel | None
    cost_problem: str | None

    @property
    def enabled(self) -> bool:
        return not self.blocking_reasons


def execution_readiness(settings: Settings, today: date) -> ExecutionReadiness:
    costs, cost_problem = None, None
    if settings.exchange_cost_schedule_path:
        try:
            costs = load_cost_schedule(settings.exchange_cost_schedule_path).resolve(settings.exchange_product, today)
        except CostScheduleError as e:
            cost_problem = str(e)
    else:
        cost_problem = "no cost schedule is configured (EXCHANGE_COST_SCHEDULE_PATH)"
    mode = settings.execution_mode
    blocking = []
    if mode == "disabled":
        blocking.append("EXECUTION_DISABLED: EXECUTION_MODE is disabled (decision D4)")
    else:
        blocking.extend(f"CONFIG_INVALID: {p}" for p in settings.execution_config_problems(today))
    if mode == "disabled" and cost_problem:
        blocking.append(f"COSTS_NOT_CONFIGURED: {cost_problem}")
    return ExecutionReadiness(blocking, costs, cost_problem)
