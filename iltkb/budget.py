"""Persistent, conservative token allowance for Gemini requests."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path

from .storage import atomic_json, read_json


class TokenBudget:
    def __init__(self, path: Path, limit: int):
        if type(limit) is not int or limit <= 0:
            raise ValueError("token_budget must be a positive integer")
        self.path = path
        self.limit = limit

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "token_budget.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = read_json(self.path) if self.path.exists() else {
                    "limit": self.limit, "spent": 0, "reserved": 0,
                }
                if data.get("limit") != self.limit:
                    raise ValueError("token_budget differs from existing ledger; use a new knowledge root")
                if any(type(data.get(key)) is not int or data[key] < 0 for key in ("spent", "reserved")):
                    raise ValueError("Invalid token budget ledger")
                yield data
                atomic_json(self.path, data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reserve(self, upper_bound: int) -> None:
        if type(upper_bound) is not int or upper_bound <= 0:
            raise ValueError("Invalid token reservation")
        with self._locked() as data:
            if data["spent"] + data["reserved"] + upper_bound > self.limit:
                raise ValueError("Token budget exhausted before Gemini request")
            data["reserved"] += upper_bound

    def settle(self, upper_bound: int, observed: int | None) -> None:
        # Unknown usage, including a failed request, consumes the full reservation.
        charged = upper_bound if type(observed) is not int or observed < 0 else observed
        with self._locked() as data:
            if data["reserved"] < upper_bound:
                raise ValueError("Token budget reservation mismatch")
            data["reserved"] -= upper_bound
            data["spent"] += charged
        if charged > upper_bound:
            raise ValueError("Gemini usage exceeded token reservation; stop and inspect budget")
