"""The Result every tool returns.

This replaces 3.x's "WRITE OK · ..." string prefixes and the regexes that read
them back. A tool states what happened as data; the host renders the receipt
line from that data; the model receives the same data. Nobody parses English.

    ok          did the requested effect happen?
    status      ok | dispatched | denied | refused | failed | cancelled
    receipt     one host-authored line, shown to the user verbatim
    content     material for the model (file text, search rows) — never shown as a receipt
    data        structured facts (paths, counts, provenance)
    undo_id     journal id when the change is reversible
    changes     how many filesystem objects changed

`dispatched` exists because handing a file to VLC is not the same fact as VLC
playing it. The receipt says exactly what the host observed, and no more.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

STATUSES = ("ok", "dispatched", "denied", "refused", "failed", "cancelled")


@dataclass
class Result:
    ok: bool
    status: str
    receipt: str
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    undo_id: Optional[str] = None
    changes: int = 0
    undo: Optional[Dict[str, Any]] = None  # reversal recipe; the journal assigns undo_id

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown result status: {self.status}")

    # ---- constructors ----------------------------------------------------
    @classmethod
    def done(cls, receipt: str, content: str = "", changes: int = 0, undo: Optional[Dict[str, Any]] = None, **data) -> "Result":
        return cls(True, "ok", receipt, content, data, None, changes, undo)

    @classmethod
    def dispatched(cls, receipt: str, **data) -> "Result":
        return cls(True, "dispatched", receipt, "", data)

    @classmethod
    def denied(cls, receipt: str, **data) -> "Result":
        """Policy said no (profile, boundary, user declined a grant)."""
        return cls(False, "denied", receipt, "", data)

    @classmethod
    def refused(cls, receipt: str, **data) -> "Result":
        """The tool itself declined: bad arguments, unsafe target, would clobber."""
        return cls(False, "refused", receipt, "", data)

    @classmethod
    def failed(cls, receipt: str, **data) -> "Result":
        return cls(False, "failed", receipt, "", data)

    @classmethod
    def cancelled(cls, receipt: str = "cancelled") -> "Result":
        return cls(False, "cancelled", receipt)

    # ---- views -----------------------------------------------------------
    def for_model(self, limit: int = 12000) -> str:
        payload: Dict[str, Any] = {"ok": self.ok, "status": self.status, "receipt": self.receipt}
        if self.data:
            payload["data"] = self.data
        if self.undo_id:
            payload["undoable"] = True
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if self.content:
            room = max(500, limit - len(text))
            body = self.content if len(self.content) <= room else self.content[:room] + f"\n… [truncated {len(self.content) - room} chars]"
            text += "\n" + body
        return text

    @property
    def mark(self) -> str:
        return {"ok": "✓", "dispatched": "↗", "denied": "⊘", "refused": "✗", "failed": "✗", "cancelled": "·"}[self.status]
