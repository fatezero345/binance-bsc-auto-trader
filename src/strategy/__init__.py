"""Pure decision layer: snapshot -> Signal.

Boundary: a strategy returns a *desire*, never an OrderIntent that has been
approved. It cannot import ``src.execution`` and cannot read
``config/risk_policy.toml`` (so it cannot reason about, or widen, its limits).
"""