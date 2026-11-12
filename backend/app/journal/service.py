"""The trading journal: manual entries, notes on automatic entries, tags, search, linked trade details, reflections.

Writes only the journal tables. Linked records are verified to belong to the user (here, with a uniform error that does
not reveal whether another user's record exists, and again by the database trigger). Linked trade details are read
live from the authoritative records when an entry is shown; the journal stores no amounts of its own.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, asc, delete, desc, func, select
from sqlalchemy.orm import Session

from app.db.enums import JournalEntryType, JournalSourceEvent, TradeOutcome
from app.db.models import (
    AgentConfigVersion,
    AgentDecision,
    AgentEvent,
    BacktestRun,
    Fill,
    Instrument,
    JournalEntry,
    JournalEntryTag,
    JournalReflection,
    JournalTag,
    Order,
    Position,
)
from app.db.scoping import owned_select
from app.explanations.facts import IST, plain
from app.explanations.trades import Episode, episodes

NOTE_FIELDS = ("notes", "entry_thesis", "exit_thesis", "market_observations", "went_well", "went_wrong", "lessons",
               "emotional_notes", "rating", "confidence")
MANUAL_ONLY_FIELDS = ("title", "occurred_at", "instrument_id", "strategy_id", "order_id", "fill_id", "position_id",
                      "agent_decision_id", "agent_event_id", "config_version_id", "backtest_run_id")
LINK_MODELS = {
    "order_id": Order, "fill_id": Fill, "position_id": Position, "agent_decision_id": AgentDecision,
    "agent_event_id": AgentEvent, "config_version_id": AgentConfigVersion, "backtest_run_id": BacktestRun,
}
MAX_TAGS = 20
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SORTS = {"occurred_at": JournalEntry.occurred_at, "created_at": JournalEntry.created_at,
         "updated_at": JournalEntry.updated_at}


class JournalError(Exception):
    """`code` is returned to the client; messages never reveal other users' data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class EntryNotFound(JournalError):
    def __init__(self):
        super().__init__("NOT_FOUND", "Journal entry not found")


def normalise_tag(raw: str) -> str:
    tag = re.sub(r"\s+", "-", raw.strip().lower())
    if not TAG_RE.match(tag):
        raise JournalError("INVALID_TAG", f"invalid tag {raw[:50]!r}: use 1-40 letters, digits, '-' or '_'")
    return tag


@dataclass(frozen=True)
class SearchFilters:
    q: str | None = None
    instrument_id: int | None = None
    strategy_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    outcome: TradeOutcome | None = None
    tags: tuple[str, ...] = ()
    entry_type: JournalEntryType | None = None
    source_event: JournalSourceEvent | None = None
    order_id: uuid.UUID | None = None
    position_id: int | None = None
    has_reflection: bool | None = None
    sort: str = "occurred_at"
    direction: str = "desc"
    limit: int = 25
    offset: int = 0


class JournalService:
    def __init__(self, db: Session, user_id: uuid.UUID):
        self.db, self.user_id = db, user_id

    # ---- reading --------------------------------------------------------------------------------------------------

    def get(self, entry_id: int, lock: bool = False) -> JournalEntry:
        q = owned_select(JournalEntry, self.user_id).where(JournalEntry.id == entry_id)
        entry = self.db.scalar(q.with_for_update() if lock else q)
        if entry is None:
            raise EntryNotFound()
        return entry

    def tags_of(self, entry_ids: list[int]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {i: [] for i in entry_ids}
        if not entry_ids:
            return out
        rows = self.db.execute(
            select(JournalEntryTag.entry_id, JournalTag.name).join(JournalTag, JournalTag.id == JournalEntryTag.tag_id)
            .where(JournalEntryTag.user_id == self.user_id, JournalEntryTag.entry_id.in_(entry_ids))
            .order_by(JournalTag.name))
        for entry_id, name in rows:
            out[entry_id].append(name)
        return out

    def reflection(self, entry_id: int) -> JournalReflection | None:
        return self.db.scalar(owned_select(JournalReflection, self.user_id).where(JournalReflection.entry_id == entry_id))

    def _filtered(self, f: SearchFilters) -> Select:
        q = owned_select(JournalEntry, self.user_id)
        if f.q:
            q = q.where(JournalEntry.search_vector.op("@@")(func.websearch_to_tsquery("english", f.q)))
        for column, value in ((JournalEntry.instrument_id, f.instrument_id), (JournalEntry.strategy_id, f.strategy_id),
                              (JournalEntry.outcome, f.outcome), (JournalEntry.entry_type, f.entry_type),
                              (JournalEntry.source_event, f.source_event), (JournalEntry.order_id, f.order_id),
                              (JournalEntry.position_id, f.position_id)):
            if value is not None:
                q = q.where(column == value)
        if f.date_from is not None:
            q = q.where(JournalEntry.session_date >= f.date_from)
        if f.date_to is not None:
            q = q.where(JournalEntry.session_date <= f.date_to)
        for tag in f.tags:
            q = q.where(select(JournalEntryTag.entry_id).join(JournalTag, JournalTag.id == JournalEntryTag.tag_id).where(
                JournalEntryTag.entry_id == JournalEntry.id, JournalEntryTag.user_id == self.user_id,
                JournalTag.name == normalise_tag(tag)).exists())
        if f.has_reflection is not None:
            exists = select(JournalReflection.id).where(JournalReflection.entry_id == JournalEntry.id).exists()
            q = q.where(exists if f.has_reflection else ~exists)
        return q

    def search(self, f: SearchFilters) -> tuple[list[JournalEntry], int]:
        q = self._filtered(f)
        total = self.db.scalar(select(func.count()).select_from(q.subquery()))
        order = desc if f.direction == "desc" else asc
        if f.sort == "relevance" and f.q:
            rank = func.ts_rank(JournalEntry.search_vector, func.websearch_to_tsquery("english", f.q))
            q = q.order_by(desc(rank), desc(JournalEntry.occurred_at), desc(JournalEntry.id))
        else:
            q = q.order_by(order(SORTS.get(f.sort, JournalEntry.occurred_at)), order(JournalEntry.id))
        return list(self.db.scalars(q.limit(f.limit).offset(f.offset))), total

    def overview(self) -> dict:
        base = select(JournalEntry.entry_type, func.count()).where(JournalEntry.user_id == self.user_id).group_by(
            JournalEntry.entry_type)
        by_type = {t.value: n for t, n in self.db.execute(base)}
        reflections = self.db.scalar(select(func.count()).select_from(JournalReflection).where(
            JournalReflection.user_id == self.user_id))
        latest = self.db.scalar(select(func.max(JournalEntry.occurred_at)).where(JournalEntry.user_id == self.user_id))
        return {"total": sum(by_type.values()), "manual": by_type.get("MANUAL", 0), "automatic": by_type.get("AUTO", 0),
                "reflections": reflections, "latest_occurred_at": latest}

    def tag_counts(self) -> list[tuple[str, int]]:
        rows = self.db.execute(
            select(JournalTag.name, func.count(JournalEntryTag.entry_id))
            .outerjoin(JournalEntryTag, JournalEntryTag.tag_id == JournalTag.id)
            .where(JournalTag.user_id == self.user_id).group_by(JournalTag.name).order_by(JournalTag.name))
        return [(name, n) for name, n in rows]

    # ---- linked records ---------------------------------------------------------------------------------------------

    def _owned(self, field: str, value):
        if value is None:
            return None
        row = self.db.scalar(owned_select(LINK_MODELS[field], self.user_id).where(LINK_MODELS[field].id == value))
        if row is None:
            raise JournalError("LINK_NOT_FOUND", f"{field} does not reference one of your records")
        return row

    def _validated_links(self, values: dict) -> dict:
        """Ownership and consistency of the links; derives the instrument from them when it is not given."""
        rows = {f: self._owned(f, values.get(f)) for f in LINK_MODELS}
        instruments = {getattr(r, "instrument_id") for f, r in rows.items()
                       if r is not None and getattr(r, "instrument_id", None) is not None}
        if values.get("instrument_id") is not None:
            if self.db.get(Instrument, values["instrument_id"]) is None:
                raise JournalError("LINK_NOT_FOUND", "instrument_id does not exist")
            instruments.add(values["instrument_id"])
        if len(instruments) > 1:
            raise JournalError("LINKS_INCONSISTENT", "the linked records belong to different instruments")
        fill, order = rows["fill_id"], rows["order_id"]
        if fill is not None and order is not None and fill.order_id != order.id:
            raise JournalError("LINKS_INCONSISTENT", "the linked fill does not belong to the linked order")
        out = dict(values)
        if fill is not None and order is None:
            out["order_id"] = fill.order_id
        out["instrument_id"] = next(iter(instruments), None)
        return out

    def trade_of(self, entry: JournalEntry) -> Episode | None:
        """The position episode an entry refers to (by fill, then order, then the open/last episode of a position)."""
        if entry.instrument_id is None:
            return None
        eps = episodes(self.db, self.user_id, entry.instrument_id)
        if entry.fill_id is not None:
            return next((e for e in eps if entry.fill_id in e.fill_ids), None)
        if entry.order_id is not None:
            return next((e for e in eps if entry.order_id in e.entry_order_ids or entry.order_id in e.exit_order_ids), None)
        if entry.position_id is not None and eps:
            return eps[-1]
        return None

    def _derive_outcome(self, entry: JournalEntry) -> TradeOutcome:
        trade = self.trade_of(entry)
        return trade.outcome if trade else TradeOutcome.NONE

    # ---- writing ----------------------------------------------------------------------------------------------------

    def create_manual(self, data: dict, tags: list[str], now: datetime) -> JournalEntry:
        if not (data.get("title") or "").strip():
            raise JournalError("INVALID", "title must not be blank")
        values = self._validated_links({k: data.get(k) for k in MANUAL_ONLY_FIELDS if k not in ("title", "occurred_at")})
        occurred = data.get("occurred_at") or now
        entry = JournalEntry(user_id=self.user_id, entry_type=JournalEntryType.MANUAL, title=data["title"].strip(),
                             occurred_at=occurred, session_date=occurred.astimezone(IST).date(), **values,
                             **{k: data.get(k) for k in NOTE_FIELDS}, created_at=now, updated_at=now)
        entry.outcome = self._derive_outcome(entry)
        self.db.add(entry)
        self.db.flush()
        self.set_tags(entry, tags)
        return entry

    def update(self, entry_id: int, patch: dict, tags: list[str] | None, now: datetime) -> JournalEntry:
        entry = self.get(entry_id, lock=True)
        system = sorted(k for k in patch if k in MANUAL_ONLY_FIELDS)
        if entry.entry_type is JournalEntryType.AUTO and system:
            raise JournalError("AUTO_ENTRY_FACTS_IMMUTABLE",
                               "the system facts of an automatic entry cannot be changed; edit its notes instead: "
                               + ", ".join(system))
        if "title" in patch and not (patch["title"] or "").strip():
            raise JournalError("INVALID", "title must not be blank")
        if entry.entry_type is JournalEntryType.MANUAL and any(k in patch for k in (*LINK_MODELS, "instrument_id")):
            current = {k: getattr(entry, k) for k in (*LINK_MODELS, "instrument_id")}
            current.update({k: v for k, v in patch.items() if k in current})
            if "instrument_id" not in patch and any(k in patch for k in LINK_MODELS):
                current["instrument_id"] = None  # re-derived from the new links
            for k, v in self._validated_links(current).items():
                setattr(entry, k, v)
        for k, v in patch.items():
            if k in NOTE_FIELDS or k in ("title", "strategy_id"):
                setattr(entry, k, v.strip() if k == "title" else v)
            elif k == "occurred_at" and v is not None:
                entry.occurred_at, entry.session_date = v, v.astimezone(IST).date()
        if entry.entry_type is JournalEntryType.MANUAL:
            entry.outcome = self._derive_outcome(entry)
        if tags is not None:
            self.set_tags(entry, tags)
        entry.updated_at = now
        self.db.flush()
        return entry

    def delete(self, entry_id: int) -> None:
        entry = self.get(entry_id, lock=True)
        if entry.entry_type is JournalEntryType.AUTO:
            raise JournalError("AUTO_ENTRY_NOT_DELETABLE",
                               "automatic entries record trading events and are kept; clear their notes instead")
        self.db.execute(delete(JournalEntryTag).where(JournalEntryTag.user_id == self.user_id,
                                                      JournalEntryTag.entry_id == entry.id))
        self.db.execute(delete(JournalReflection).where(JournalReflection.user_id == self.user_id,
                                                        JournalReflection.entry_id == entry.id))
        self.db.delete(entry)
        self.db.flush()

    def set_tags(self, entry: JournalEntry, raw: list[str]) -> list[str]:
        names = sorted({normalise_tag(t) for t in raw})
        if len(names) > MAX_TAGS:
            raise JournalError("TOO_MANY_TAGS", f"at most {MAX_TAGS} tags per entry")
        existing = {t.name: t for t in self.db.scalars(owned_select(JournalTag, self.user_id).where(JournalTag.name.in_(names)))}
        for name in names:
            if name not in existing:
                tag = JournalTag(user_id=self.user_id, name=name)
                self.db.add(tag)
                self.db.flush()
                existing[name] = tag
        self.db.execute(delete(JournalEntryTag).where(JournalEntryTag.user_id == self.user_id,
                                                      JournalEntryTag.entry_id == entry.id))
        for name in names:
            self.db.add(JournalEntryTag(user_id=self.user_id, entry_id=entry.id, tag_id=existing[name].id))
        self.db.flush()
        return names

    # ---- reflections ------------------------------------------------------------------------------------------------

    def closed_trade(self, entry: JournalEntry) -> Episode | None:
        if entry.source_event is JournalSourceEvent.TRADE_CLOSED:
            return self.trade_of(entry)
        trade = self.trade_of(entry)
        return trade if trade is not None and trade.closed else None

    def save_reflection(self, entry_id: int, data: dict, now: datetime) -> tuple[JournalReflection, bool]:
        entry = self.get(entry_id, lock=True)
        if self.closed_trade(entry) is None:
            raise JournalError("TRADE_NOT_CLOSED",
                               "a reflection needs an entry linked to a closed trade (link an order, fill or position)")
        row = self.reflection(entry.id)
        created = row is None
        if created:
            row = JournalReflection(user_id=self.user_id, entry_id=entry.id, created_at=now)
            self.db.add(row)
        for k, v in data.items():
            setattr(row, k, v)
        row.updated_at = now
        self.db.flush()
        return row, created

    # ---- linked trade details (live, authoritative) -------------------------------------------------------------------

    def linked(self, entry: JournalEntry) -> dict:
        out: dict = {"instrument": None, "order": None, "fills": [], "position": None, "trade": None,
                     "agent_decision": None, "source": "read live from orders, fills, positions and agent decisions"}
        if entry.instrument_id is not None:
            inst = self.db.get(Instrument, entry.instrument_id)
            out["instrument"] = {"id": entry.instrument_id, "symbol": inst.symbol if inst else None}
        if entry.order_id is not None:
            o = self.db.scalar(owned_select(Order, self.user_id).where(Order.id == entry.order_id))
            if o is not None:
                out["order"] = plain({"id": o.id, "client_order_id": o.client_order_id, "source": o.source, "side": o.side,
                                      "order_type": o.order_type, "quantity": o.quantity, "filled_quantity": o.filled_quantity,
                                      "status": o.status, "avg_fill_price": o.avg_fill_price, "reject_reason": o.reject_reason,
                                      "accepted_at": o.accepted_at})
        trade = self.trade_of(entry)
        fill_ids = trade.fill_ids if trade else ([entry.fill_id] if entry.fill_id else [])
        if fill_ids:
            fills = self.db.scalars(owned_select(Fill, self.user_id).where(Fill.id.in_(fill_ids)).order_by(Fill.price_as_of, Fill.id))
            out["fills"] = [plain({"id": f.id, "order_id": f.order_id, "side": f.side, "quantity": f.quantity,
                                   "price": f.price, "fee": f.fee, "realized_pnl": f.realized_pnl,
                                   "price_as_of": f.price_as_of, "price_basis": f.price_basis,
                                   "execution_policy": f.execution_policy}) for f in fills]
        if trade is not None:
            out["trade"] = plain({"status": "CLOSED" if trade.closed else "OPEN", "outcome": trade.outcome,
                                  "opened_at": trade.opened_at, "closed_at": trade.closed_at,
                                  "realized_pnl": trade.realized_pnl, "fees": trade.fees,
                                  "net_realized_pnl": trade.net_realized_pnl, "strategy_id": trade.strategy_id,
                                  "exit_sources": trade.exit_sources})
        pos_id = entry.position_id
        if pos_id is not None:
            p = self.db.scalar(owned_select(Position, self.user_id).where(Position.id == pos_id))
            if p is not None:
                out["position"] = plain({"id": p.id, "quantity_now": p.quantity, "avg_entry_price": p.avg_entry_price,
                                         "realized_pnl_to_date": p.realized_pnl, "fees_paid_to_date": p.fees_paid})
        if entry.agent_decision_id is not None:
            d = self.db.scalar(owned_select(AgentDecision, self.user_id).where(AgentDecision.id == entry.agent_decision_id))
            if d is not None:
                out["agent_decision"] = plain({"id": d.id, "kind": d.kind, "outcome": d.outcome, "strategy_id": d.strategy_id,
                                               "reason": d.reason, "action": d.action, "bar_start": d.bar_start,
                                               "order_status": d.order_status})
        return out

