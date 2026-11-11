"""Hybrid risk management: user-defined hard limits combined with strategy-specific rules.

`engine` is pure and deterministic (no database, no clock, no LLM). `service` loads settings
and account state and persists every decision.
"""
