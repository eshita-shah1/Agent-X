"""Mock Exchange: simulated order execution, cash ledger and positions.

The exchange never generates market prices. It consumes price observations through the
`ExecutionPriceSource` contract and only after they pass the `ExecutionPriceGate` (decision D4).
"""
