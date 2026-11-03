"""Append-only JSONL ledger plus reconciliation against read-only holdings.

Every state transition is a line; corrections are new lines, never edits.
"""