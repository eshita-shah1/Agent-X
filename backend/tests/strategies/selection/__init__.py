"""Hand-built histories for the selection tests.

Day 1 (2026-11-02) is a full 75-bar session and day 2 (2026-11-03) supplies the checkpoint bars. ADX(14) needs 28
bars, so day 1 provides the warm-up (ADX runs across sessions).

  trending(): every bar 0.5 higher, high/low moving with it -> +DI 50, -DI 0, DX 100 -> ADX 100 (TREND)
  flat():     every bar identical -> TR 0 -> DI 0, DX 0 -> ADX 0 (RANGE)
  mixed():    39 rising bars, then alternating 0 / +0.5 -> ADX(14) = 22.54 at the day-2 checkpoint (UNDETERMINED)
"""

from app.strategies import EMACrossoverConfig, ORBConfig, RSIBollingerConfig, SupertrendConfig, VWAPConfig
from app.strategies.selection import Candidate
from tests.strategies import DAY, NEXT_DAY, THIRD_DAY, pbar, slot_of


def trending(day2_slots=range(3), start=100.0):
    bars = [pbar(DAY, i, start + 0.5 * i) for i in range(75)]
    return bars + [pbar(NEXT_DAY, s, start + 0.5 * (75 + s)) for s in day2_slots]


def flat(day2_slots=range(3), price=100.0):
    return [pbar(DAY, i, price) for i in range(75)] + [pbar(NEXT_DAY, s, price) for s in day2_slots]


def mixed(day2_slots=range(3)):
    def close(i):
        return 100 + 0.5 * i if i < 39 else 100 + 0.5 * 39 + (0.5 if i % 2 else 0)

    return [pbar(DAY, i, close(i)) for i in range(75)] + [pbar(NEXT_DAY, s, close(75 + s)) for s in day2_slots]


def later(day=NEXT_DAY, start="09:30", end="10:30", price=100.0):
    return [pbar(day, s, price) for s in range(slot_of(start), slot_of(end) + 1)]


ORB = Candidate("orb-1", "ORB", ORBConfig(), 1)
EMA = Candidate("ema-1", "EMA_CROSSOVER", EMACrossoverConfig(), 2)
SUPERTREND = Candidate("st-1", "SUPERTREND", SupertrendConfig(), 3)
RSI_BB = Candidate("rsi-1", "RSI_BOLLINGER", RSIBollingerConfig(), 4)
VWAP = Candidate("vwap-1", "VWAP", VWAPConfig(), 5)
ALL = (ORB, EMA, SUPERTREND, RSI_BB, VWAP)

__all__ = ["ALL", "DAY", "EMA", "NEXT_DAY", "ORB", "RSI_BB", "SUPERTREND", "THIRD_DAY", "VWAP", "flat", "later", "mixed", "trending"]
