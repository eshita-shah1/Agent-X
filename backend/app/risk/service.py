"""Risk persistence: user limits, risk state (kill switch, equity references) and snapshots."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.enums import OrderSide
from app.db.models import AuditEvent, RiskSettings, RiskState, VirtualAccount
from app.db.scoping import owned_select
from app.exchange.portfolio import AccountValuation, PortfolioReader
from app.exchange.service import MockExchange, lock_account
from app.risk.engine import AccountRiskSnapshot, UserRiskLimits

# NSE trading dates are Indian calendar dates (a fact about the exchange, not a parameter).
EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")

_LIMIT_FIELDS = (
    "max_risk_per_trade_pct",
    "max_position_allocation_pct",
    "max_portfolio_exposure_pct",
    "max_daily_loss_pct",
    "max_drawdown_pct",
    "max_position_quantity",
    "resize_allowed",
)


@dataclass(frozen=True)
class RiskSettingsInput:
    max_risk_per_trade_pct: Decimal
    max_position_allocation_pct: Decimal
    max_portfolio_exposure_pct: Decimal
    max_daily_loss_pct: Decimal
    max_drawdown_pct: Decimal
    max_position_quantity: int | None
    resize_allowed: bool


@dataclass(frozen=True)
class RiskStatus:
    state: RiskState
    equity: Decimal | None
    drawdown_pct: Decimal | None
    daily_loss_pct: Decimal | None
    breaches: list[str]


def _settings_dict(s: RiskSettings) -> dict:
    return {f: (str(v) if isinstance(v := getattr(s, f), Decimal) else v) for f in _LIMIT_FIELDS}


class RiskService:
    def __init__(self, db: Session, portfolio: PortfolioReader, clock: Clock):
        self.db = db
        self.portfolio = portfolio
        self.clock = clock

    # ---- settings ------------------------------------------------------------------------

    def get_settings(self, user_id: uuid.UUID) -> RiskSettings | None:
        return self.db.scalar(owned_select(RiskSettings, user_id))

    def limits(self, user_id: uuid.UUID) -> UserRiskLimits | None:
        s = self.get_settings(user_id)
        if s is None:
            return None
        return UserRiskLimits(**{f: getattr(s, f) for f in _LIMIT_FIELDS})

    def save_settings(self, user_id: uuid.UUID, data: RiskSettingsInput) -> RiskSettings:
        lock_account(self.db, user_id)
        existing = self.get_settings(user_id)
        before = _settings_dict(existing) if existing else None
        if existing is None:
            existing = RiskSettings(user_id=user_id, **data.__dict__)
            self.db.add(existing)
        else:
            for f in _LIMIT_FIELDS:
                setattr(existing, f, getattr(data, f))
        self.db.flush()
        self.db.add(
            AuditEvent(
                user_id=user_id,
                event_type="risk.settings_saved",
                details={"before": before, "after": _settings_dict(existing)},
            )
        )
        self.db.flush()
        return existing

    # ---- state ---------------------------------------------------------------------------

    def get_state(self, user_id: uuid.UUID, create: bool = True) -> RiskState | None:
        state = self.db.scalar(owned_select(RiskState, user_id))
        if state is None and create:
            state = RiskState(user_id=user_id, kill_switch_engaged=False)
            self.db.add(state)
            self.db.flush()
        return state

    def set_kill_switch(self, account: VirtualAccount, engaged: bool, reason: str, exchange: MockExchange) -> RiskState:
        """Engaging blocks new entries and cancels every open order in the same transaction.
        Exits of held positions remain possible (they reduce risk)."""
        lock_account(self.db, account.user_id)
        state = self.get_state(account.user_id)
        assert state is not None
        state.kill_switch_engaged = engaged
        state.kill_switch_reason = reason
        state.kill_switch_changed_at = self.clock()
        cancelled = exchange.cancel_all_open(account, f"KILL_SWITCH_ENGAGED: {reason}") if engaged else []
        self.db.add(
            AuditEvent(
                user_id=account.user_id,
                event_type="risk.kill_switch_engaged" if engaged else "risk.kill_switch_released",
                details={"reason": reason, "cancelled_orders": [str(o.id) for o in cancelled]},
            )
        )
        self.db.flush()
        return state

    def session_date(self) -> date:
        return self.clock().astimezone(EXCHANGE_TZ).date()

    def observe_equity(self, account: VirtualAccount, valuation: AccountValuation) -> RiskState:
        """Update the peak-equity and start-of-day references from a determined equity.

        Called on every order submission, every `process_open_orders` run and every `refresh` (GET
        /risk/state, POST /exchange/process). Limitation (documented): the day reference is the equity at the
        first evaluation on a session date. Phase 5 must call `refresh` at session start and on every cycle;
        `last_equity_at` shows how current the references are.
        """
        state = self.get_state(account.user_id)
        assert state is not None
        if state.peak_equity is None:
            state.peak_equity = account.initial_capital
        equity = valuation.equity
        if equity is not None:
            if equity > state.peak_equity:
                state.peak_equity = equity
            today = self.session_date()
            if state.day_session_date != today:
                state.day_session_date = today
                state.day_start_equity = equity
            state.last_equity = equity
            state.last_equity_at = self.clock()
        self.db.flush()
        return state

    def refresh(self, account: VirtualAccount, limits: UserRiskLimits | None = None) -> "RiskStatus":
        """Re-value the account and update the risk references without needing an order. Reports the current
        drawdown and daily loss and which hard limits are breached (breaches block new entries; exits stay
        allowed)."""
        valuation = self.portfolio.valuation(account)
        state = self.observe_equity(account, valuation)
        limits = limits if limits is not None else self.limits(account.user_id)
        equity = valuation.equity
        dd = dl = None
        if equity is not None and state.peak_equity:
            dd = (state.peak_equity - equity) / state.peak_equity * 100
        if equity is not None and state.day_start_equity:
            dl = (state.day_start_equity - equity) / state.day_start_equity * 100
        breaches = []
        if state.kill_switch_engaged:
            breaches.append("KILL_SWITCH_ENGAGED")
        if equity is None:
            breaches.append(f"EQUITY_UNDETERMINED: cannot mark instruments {valuation.unmarked_instrument_ids}")
        if limits is not None:
            if dd is not None and dd >= limits.max_drawdown_pct:
                breaches.append(f"MAX_DRAWDOWN_BREACHED: {dd:.4f}% >= {limits.max_drawdown_pct}%")
            if dl is not None and dl >= limits.max_daily_loss_pct:
                breaches.append(f"DAILY_LOSS_LIMIT_BREACHED: {dl:.4f}% >= {limits.max_daily_loss_pct}%")
        else:
            breaches.append("RISK_SETTINGS_NOT_CONFIGURED")
        return RiskStatus(state, equity, dd, dl, breaches)

    def snapshot(self, account: VirtualAccount, instrument_id: int) -> AccountRiskSnapshot:
        valuation = self.portfolio.valuation(account)
        state = self.observe_equity(account, valuation)
        pos_val = next((v for v in valuation.positions if v.position.instrument_id == instrument_id), None)
        position = self.portfolio.position(account.user_id, instrument_id)
        held = position.quantity if position else 0
        open_sells = self.portfolio.open_orders(account.user_id, instrument_id, OrderSide.SELL)
        open_buys = self.portfolio.open_orders(account.user_id, instrument_id, OrderSide.BUY)
        if valuation.equity is None:
            detail = f"cannot mark open positions in instruments {valuation.unmarked_instrument_ids}"
        else:
            detail = f"equity {valuation.equity}"
        return AccountRiskSnapshot(
            kill_switch_engaged=state.kill_switch_engaged,
            equity=valuation.equity,
            equity_detail=detail,
            available_cash=valuation.available_cash,
            gross_exposure=valuation.gross_exposure,
            peak_equity=state.peak_equity,
            day_start_equity=state.day_start_equity,
            position_quantity=held,
            position_value=pos_val.market_value if pos_val else Decimal(0),
            sellable_quantity=held - sum(o.quantity - o.filled_quantity for o in open_sells),
            open_buy_quantity=sum(o.quantity - o.filled_quantity for o in open_buys),
            open_buy_commitment=sum((o.reserved_cash for o in open_buys), Decimal(0)),
        )
