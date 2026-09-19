from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key

from app.utils.paths import ENV_PATH, PROJECT_ROOT, ensure_runtime_dirs


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_collection_mode(value: str | None) -> str:
    text = (value or "automation").strip().lower()
    if text in {"api", "openapi", "key"}:
        return "api"
    return "automation"


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip() not in {"0", "false", "False", "no", "NO"}


@dataclass
class AppConfig:
    api_key: str
    api_keys: list[str]
    api_base: str
    plan: str
    rate: float
    capacity: float
    target_utilization: float
    initial_utilization: float
    reset_safety_margin: float
    shared_limit_detection: bool
    max_api_wait: float
    api_workers: int
    token_info_ttl_seconds: int
    token_pool_ttl_seconds: int
    wallet_stats_ttl_seconds: int
    rate_limit_max_retries: int
    export_combined: bool = True
    export_per_wallet: bool = True
    moralis_api_key: str = ""
    moralis_base: str = "https://solana-gateway.moralis.io"
    helius_api_key: str = ""
    helius_api_keys: list[str] = field(default_factory=list)
    helius_rpc_url: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    verification_mode: str = "BALANCED"
    enable_gmgn_deep_history_fallback: bool = False
    enable_gmgn: bool = True
    enable_moralis: bool = False
    moralis_api_keys: list[str] = field(default_factory=list)
    helius_target_rps: float = 8.0
    helius_monthly_credit_budget: int = 1_000_000
    helius_api_workers: int = 4
    collection_mode: str = "automation"
    browser_headed: bool = True
    gmgn_qr_login_timeout: float = 180.0

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_keys)

    @property
    def effective_rate(self) -> float:
        return self.rate * self.target_utilization


def _parse_keys(values: dict[str, str]) -> list[str]:
    multi = (values.get("GMGN_API_KEYS") or "").strip()
    keys: list[str] = []
    if multi:
        for part in multi.replace("\n", ",").split(","):
            item = part.strip()
            if item:
                keys.append(item)
    single = (values.get("GMGN_API_KEY") or "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _looks_like_helius_key(key: str) -> bool:
    text = (key or "").strip()
    if len(text) != 36:
        return False
    parts = text.split("-")
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    hex_chars = set("0123456789abcdefABCDEF")
    return all(ch in hex_chars for part in parts for ch in part)


def _rehome_helius_keys(moralis_keys: list[str], helius_keys: list[str]) -> tuple[list[str], list[str]]:
    misplaced = [k for k in moralis_keys if _looks_like_helius_key(k)]
    if not misplaced:
        return moralis_keys, helius_keys
    merged = list(helius_keys)
    for key in misplaced:
        if key not in merged:
            merged.append(key)
    kept = [k for k in moralis_keys if k not in misplaced]
    return kept, merged


def _parse_named_keys(values: dict[str, str], multi_key: str, single_key: str) -> list[str]:
    multi = (values.get(multi_key) or "").strip()
    keys: list[str] = []
    if multi:
        for part in multi.replace("\n", ",").split(","):
            item = part.strip()
            if item:
                keys.append(item)
    single = (values.get(single_key) or "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _read_env_file() -> dict[str, str]:
    ensure_runtime_dirs()
    load_dotenv(ENV_PATH, override=False)
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    return {str(k): str(v) for k, v in values.items() if k and v is not None}


def load_config() -> AppConfig:
    values = _read_env_file()
    keys = _parse_keys(values)
    moralis_keys = _parse_named_keys(values, "MORALIS_API_KEYS", "MORALIS_API_KEY")
    helius_keys = _parse_named_keys(values, "HELIUS_API_KEYS", "HELIUS_API_KEY")
    moralis_keys, helius_keys = _rehome_helius_keys(moralis_keys, helius_keys)
    api_base = (values.get("GMGN_API_BASE") or "https://openapi.gmgn.ai").rstrip("/")
    plan = (values.get("GMGN_PLAN") or "Free").strip() or "Free"
    rate = float(values.get("GMGN_KEY_RATE") or values.get("GMGN_RATE_LIMIT_RATE") or values.get("GMGN_RATE") or 5)
    capacity = float(values.get("GMGN_KEY_CAPACITY") or values.get("GMGN_RATE_LIMIT_CAPACITY") or values.get("GMGN_CAPACITY") or 5)
    max_target = _clamp(float(values.get("GMGN_MAX_TARGET_UTILIZATION") or values.get("GMGN_TARGET_UTILIZATION") or 0.75), 0.30, 0.80)
    initial = _clamp(float(values.get("GMGN_INITIAL_UTILIZATION") or 0.60), 0.30, max_target)
    margin = float(values.get("GMGN_RESET_SAFETY_MARGIN") or 3)
    workers = int(values.get("GMGN_API_WORKERS") or 4)
    workers = max(2, min(8, workers))
    ttl = int(values.get("TOKEN_INFO_CACHE_TTL") or values.get("TOKEN_INFO_TTL_SECONDS") or 43200)
    pool_ttl = int(values.get("TOKEN_POOL_CACHE_TTL") or 1800)
    stats_ttl = int(values.get("WALLET_STATS_CACHE_TTL") or 180)
    retries = int(values.get("GMGN_429_MAX_RETRIES") or 8)
    export_combined = (values.get("EXPORT_COMBINED") or "1").strip() not in {"0", "false", "False"}
    export_per_wallet = (values.get("EXPORT_PER_WALLET") or "1").strip() not in {"0", "false", "False"}
    return AppConfig(
        api_key=keys[0] if keys else "",
        api_keys=keys,
        api_base=api_base,
        plan=plan,
        rate=rate,
        capacity=capacity,
        target_utilization=max_target,
        initial_utilization=initial,
        reset_safety_margin=max(0.0, margin),
        shared_limit_detection=_truthy(values.get("GMGN_SHARED_LIMIT_DETECTION"), True),
        max_api_wait=float(values.get("GMGN_MAX_API_WAIT") or 900),
        api_workers=workers,
        token_info_ttl_seconds=ttl,
        token_pool_ttl_seconds=pool_ttl,
        wallet_stats_ttl_seconds=stats_ttl,
        rate_limit_max_retries=max(0, retries),
        export_combined=export_combined,
        export_per_wallet=export_per_wallet,
        moralis_api_key=moralis_keys[0] if moralis_keys else "",
        moralis_api_keys=moralis_keys,
        moralis_base=(values.get("MORALIS_API_BASE") or "https://solana-gateway.moralis.io").rstrip("/"),
        helius_api_key=helius_keys[0] if helius_keys else "",
        helius_api_keys=helius_keys,
        helius_rpc_url=(values.get("HELIUS_RPC_URL") or "").strip(),
        solana_rpc_url=(values.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").rstrip("/"),
        verification_mode=(values.get("VERIFICATION_MODE") or "BALANCED").strip() or "BALANCED",
        enable_gmgn_deep_history_fallback=_truthy(values.get("ENABLE_GMGN_DEEP_HISTORY_FALLBACK"), False),
        enable_gmgn=_truthy(values.get("ENABLE_GMGN"), True),
        enable_moralis=bool(moralis_keys) and _truthy(values.get("ENABLE_MORALIS"), True),
        helius_target_rps=float(values.get("HELIUS_TARGET_RPS") or 8),
        helius_monthly_credit_budget=int(values.get("HELIUS_MONTHLY_CREDIT_BUDGET") or 1_000_000),
        helius_api_workers=max(1, min(8, int(values.get("HELIUS_API_WORKERS") or 4))),
        collection_mode=_parse_collection_mode(values.get("GMGN_COLLECTION_MODE")),
        browser_headed=_truthy(values.get("GMGN_BROWSER_HEADED"), True),
        gmgn_qr_login_timeout=float(values.get("GMGN_QR_LOGIN_TIMEOUT") or 180),
    )


def save_api_settings(
    api_key: str,
    api_base: str = "https://openapi.gmgn.ai",
    plan: str = "Free",
    rate: float = 5,
    capacity: float = 5,
    api_keys: list[str] | None = None,
    target_utilization: float = 0.75,
    api_workers: int = 4,
    initial_utilization: float = 0.60,
    reset_safety_margin: float = 3.0,
    shared_limit_detection: bool = True,
    extra: dict[str, str] | None = None,
) -> None:
    ensure_runtime_dirs()
    if not ENV_PATH.exists():
        ENV_PATH.write_text(
            "GMGN_API_KEY=\nGMGN_API_KEYS=\nGMGN_API_BASE=https://openapi.gmgn.ai\n",
            encoding="utf-8",
        )
    keys = api_keys if api_keys is not None else ([api_key.strip()] if api_key.strip() else [])
    keys = [k.strip() for k in keys if k and k.strip()]
    primary = keys[0] if keys else api_key.strip()
    target = _clamp(float(target_utilization), 0.30, 0.80)
    initial = _clamp(float(initial_utilization), 0.30, target)
    set_key(str(ENV_PATH), "GMGN_API_KEY", primary, quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_API_KEYS", ",".join(keys), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_API_BASE", api_base.rstrip("/"), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_PLAN", plan, quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_RATE", str(rate), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_CAPACITY", str(capacity), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_KEY_RATE", str(rate), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_KEY_CAPACITY", str(capacity), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_RATE_LIMIT_RATE", str(rate), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_RATE_LIMIT_CAPACITY", str(capacity), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_TARGET_UTILIZATION", str(target), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_MAX_TARGET_UTILIZATION", str(target), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_INITIAL_UTILIZATION", str(initial), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_RESET_SAFETY_MARGIN", str(float(reset_safety_margin)), quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_SHARED_LIMIT_DETECTION", "true" if shared_limit_detection else "false", quote_mode="never")
    set_key(str(ENV_PATH), "GMGN_API_WORKERS", str(max(2, min(8, int(api_workers)))), quote_mode="never")
    if extra:
        for key, value in extra.items():
            set_key(str(ENV_PATH), key, str(value), quote_mode="never")
    load_dotenv(ENV_PATH, override=True)


def project_root() -> Path:
    return PROJECT_ROOT
