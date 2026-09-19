from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from app.utils.paths import COOKIE_DIR, ensure_runtime_dirs

POOL_INDEX = "pool.json"
HEALTHY = "HEALTHY"
EXPIRED = "EXPIRED"
DISABLED = "DISABLED"

AUTH_COOKIE_HINTS = ("token", "auth", "sess", "sid", "jwt", "user", "login", "access", "uid", "account")
ANALYTICS_COOKIE_PREFIXES = ("_ga", "_gid", "_gat", "_fbp", "_ttp", "_clck", "_clsk", "amp_")
JUNK_COOKIE_NAMES = ("_did", "deviceid", "device_id", "__cf_bm", "__cf_clearance", "cf_clearance")
JUNK_COOKIE_PREFIXES = ("__cf", "_cf", "cf_")


def _is_junk_cookie(name: str) -> bool:
    lname = name.lower()
    if lname in JUNK_COOKIE_NAMES:
        return True
    if any(lname == p or lname.startswith(p) for p in ANALYTICS_COOKIE_PREFIXES):
        return True
    return any(lname.startswith(p) for p in JUNK_COOKIE_PREFIXES)


def has_login_cookies(storage_state: dict[str, Any] | None) -> bool:
    """True only when gmgn.ai has a real session cookie, not device/CF/analytics crumbs."""
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return False
    first_party: list[str] = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lower()
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value:
            continue
        if "gmgn" not in domain:
            continue
        lname = name.lower()
        if _is_junk_cookie(lname):
            continue
        first_party.append(lname)
        if any(hint in lname for hint in AUTH_COOKIE_HINTS):
            return True
    return len(first_party) >= 2


@dataclass
class CookieAccount:
    id: str
    state: str = HEALTHY
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    last_validated_at: float = 0.0
    fail_count: int = 0
    label: str = ""
    storage_file: str = ""

    @property
    def masked(self) -> str:
        suffix = (self.id or "????")[-4:]
        return f"CK {self.id} ****{suffix}"

    def storage_path(self, root: Path) -> Path:
        name = self.storage_file or f"{self.id}.json"
        return root / name

    def is_usable(self) -> bool:
        return self.state == HEALTHY


class CookiePool:
    """Persist Playwright storage_state files and pick a reusable GMGN session."""

    def __init__(self, root: Path | None = None) -> None:
        ensure_runtime_dirs()
        self.root = Path(root) if root is not None else COOKIE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._accounts: list[CookieAccount] = []
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._accounts = self._read_index()

    def all(self) -> list[CookieAccount]:
        with self._lock:
            return list(self._accounts)

    def usable(self) -> list[CookieAccount]:
        with self._lock:
            return [a for a in self._accounts if self._ready(a)]

    def has_usable(self) -> bool:
        return bool(self.usable())

    def _ready(self, account: CookieAccount) -> bool:
        if not account.is_usable() or not account.storage_path(self.root).exists():
            return False
        return has_login_cookies(self.load_storage(account))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(a) for a in self._accounts]

    def acquire(self) -> Optional[CookieAccount]:
        """Least-recently-used account that actually contains login cookies."""
        with self._lock:
            usable = [a for a in self._accounts if self._ready(a)]
            if not usable:
                return None
            usable.sort(key=lambda a: (a.last_used_at, a.created_at))
            picked = usable[0]
            picked.last_used_at = time.time()
            self._write_index()
            return picked

    def load_storage(self, account: CookieAccount) -> Optional[dict[str, Any]]:
        path = account.storage_path(self.root)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save_storage(
        self,
        storage_state: dict[str, Any],
        *,
        account: CookieAccount | None = None,
        label: str = "",
    ) -> CookieAccount:
        now = time.time()
        with self._lock:
            if account is None:
                account = CookieAccount(
                    id=self._next_id(),
                    created_at=now,
                    label=label,
                )
                account.storage_file = f"{account.id}.json"
                self._accounts.append(account)
            account.state = HEALTHY
            account.updated_at = now
            account.last_used_at = now
            account.last_validated_at = now
            account.fail_count = 0
            if label:
                account.label = label
            path = account.storage_path(self.root)
            path.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_index()
            return account

    def mark_valid(self, account: CookieAccount) -> None:
        with self._lock:
            account.state = HEALTHY
            account.last_validated_at = time.time()
            account.last_used_at = time.time()
            account.fail_count = 0
            self._write_index()

    def mark_expired(self, account: CookieAccount, reason: str = "") -> None:
        with self._lock:
            account.state = EXPIRED
            account.fail_count += 1
            account.updated_at = time.time()
            if reason:
                account.label = account.label or reason[:80]
            self._write_index()

    def disable(self, account_id: str) -> None:
        with self._lock:
            for account in self._accounts:
                if account.id == account_id:
                    account.state = DISABLED
                    account.updated_at = time.time()
            self._write_index()

    def purge_expired(self) -> int:
        removed = 0
        with self._lock:
            kept: list[CookieAccount] = []
            for account in self._accounts:
                if account.state == EXPIRED:
                    path = account.storage_path(self.root)
                    if path.exists():
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    removed += 1
                    continue
                kept.append(account)
            self._accounts = kept
            self._write_index()
        return removed

    def _next_id(self) -> str:
        existing = {a.id for a in self._accounts}
        for i in range(1, 10_000):
            candidate = f"ck-{i:03d}"
            if candidate not in existing:
                return candidate
        return f"ck-{uuid.uuid4().hex[:8]}"

    def _index_path(self) -> Path:
        return self.root / POOL_INDEX

    def _read_index(self) -> list[CookieAccount]:
        path = self._index_path()
        if not path.exists():
            recovered = self._recover_from_files()
            if recovered:
                self._accounts = recovered
                self._write_index()
            return recovered
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._recover_from_files()
        rows = raw.get("accounts") if isinstance(raw, dict) else raw
        accounts: list[CookieAccount] = []
        if not isinstance(rows, list):
            return accounts
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            accounts.append(
                CookieAccount(
                    id=str(row.get("id")),
                    state=str(row.get("state") or HEALTHY),
                    created_at=float(row.get("created_at") or 0),
                    updated_at=float(row.get("updated_at") or 0),
                    last_used_at=float(row.get("last_used_at") or 0),
                    last_validated_at=float(row.get("last_validated_at") or 0),
                    fail_count=int(row.get("fail_count") or 0),
                    label=str(row.get("label") or ""),
                    storage_file=str(row.get("storage_file") or f"{row.get('id')}.json"),
                )
            )
        return accounts

    def _recover_from_files(self) -> list[CookieAccount]:
        accounts: list[CookieAccount] = []
        for path in sorted(self.root.glob("ck-*.json")):
            accounts.append(
                CookieAccount(
                    id=path.stem,
                    storage_file=path.name,
                    created_at=path.stat().st_mtime,
                    updated_at=path.stat().st_mtime,
                )
            )
        return accounts

    def _write_index(self) -> None:
        payload = {"accounts": [asdict(a) for a in self._accounts]}
        tmp = self._index_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._index_path())
