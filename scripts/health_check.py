from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.clob import build_clob_client, get_collateral_status
from api.polygon import PolygonClient
from bot.config import EnvironmentConfig, load_environment


@dataclass(frozen=True, slots=True)
class HealthCheckSummary:
    signature_type: int
    clob_host: str
    expected_chain_id: int
    signer_address: str
    funder_address: str
    signer_matches_funder: bool
    polygon_rpc_connected: bool
    polygon_chain_id: int | None
    signer_native_balance: float | None
    funder_native_balance: float | None
    funder_usdc_balance: float | None
    clob_server_time_ok: bool
    l2_auth_ok: bool
    balance_allowance_ok: bool
    clob_collateral_balance_usdc: float | None
    clob_allowances_usdc: dict[str, float] | None
    notes: list[str]


def main() -> None:
    try:
        env = load_environment()
        ensure_required_env(env)
        summary = run_health_check(env)
    except Exception as exc:
        print(f"health_check_error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(asdict(summary), indent=2))


def run_health_check(env: EnvironmentConfig) -> HealthCheckSummary:
    signer_address = Account.from_key(env.poly_private_key).address
    funder_address = Account.from_key(env.poly_wallet_address).address if False else env.poly_wallet_address
    if funder_address is None:
        raise ValueError("POLY_WALLET_ADDRESS is required")

    notes: list[str] = []
    signer_matches_funder = signer_address.lower() == funder_address.lower()

    if env.poly_signature_type == 0 and not signer_matches_funder:
        raise ValueError(
            "EOA mode requires POLY_PRIVATE_KEY to resolve to POLY_WALLET_ADDRESS"
        )
    if env.poly_signature_type in {1, 2}:
        if signer_matches_funder:
            notes.append(
                "Signer and funder match. That is unusual for proxy/safe mode but not fatal."
            )
        else:
            notes.append(
                "Signer and funder differ, which is expected for POLY_PROXY / SAFE setups."
            )

    polygon = PolygonClient(env.polygon_rpc_url or "")
    rpc_connected = polygon.is_connected()
    chain_id = polygon.get_chain_id() if rpc_connected else None
    if chain_id is not None and chain_id != env.poly_chain_id:
        raise ValueError(
            f"Polygon RPC chain_id mismatch: expected {env.poly_chain_id}, got {chain_id}"
        )

    signer_native_balance = polygon.get_native_balance(signer_address) if rpc_connected else None
    funder_native_balance = polygon.get_native_balance(funder_address) if rpc_connected else None
    funder_usdc_balance = polygon.get_usdc_balance(funder_address) if rpc_connected else None

    clob_client = build_clob_client(env, include_api_creds=False)
    clob_server_time_ok = False
    l2_auth_ok = False
    balance_allowance_ok = False
    clob_collateral_balance_usdc = None
    clob_allowances_usdc = None

    server_time = clob_client.get_server_time()
    clob_server_time_ok = bool(server_time)

    if env.poly_api_key and env.poly_api_secret and env.poly_api_passphrase:
        clob_client = build_clob_client(env, include_api_creds=True)
        api_keys = clob_client.get_api_keys()
        l2_auth_ok = _looks_like_valid_api_keys_response(api_keys)

        collateral_status = get_collateral_status(clob_client, env)
        balance_allowance_ok = True
        clob_collateral_balance_usdc = collateral_status.balance_usdc
        clob_allowances_usdc = collateral_status.allowances_usdc
    else:
        notes.append(
            "POLY_API_KEY / SECRET / PASSPHRASE are missing, so L2 auth was not checked."
        )

    return HealthCheckSummary(
        signature_type=env.poly_signature_type,
        clob_host=env.poly_clob_host,
        expected_chain_id=env.poly_chain_id,
        signer_address=signer_address,
        funder_address=funder_address,
        signer_matches_funder=signer_matches_funder,
        polygon_rpc_connected=rpc_connected,
        polygon_chain_id=chain_id,
        signer_native_balance=signer_native_balance,
        funder_native_balance=funder_native_balance,
        funder_usdc_balance=funder_usdc_balance,
        clob_server_time_ok=clob_server_time_ok,
        l2_auth_ok=l2_auth_ok,
        balance_allowance_ok=balance_allowance_ok,
        clob_collateral_balance_usdc=clob_collateral_balance_usdc,
        clob_allowances_usdc=clob_allowances_usdc,
        notes=notes,
    )


def ensure_required_env(env: EnvironmentConfig) -> None:
    missing: list[str] = []
    if not env.poly_private_key:
        missing.append("POLY_PRIVATE_KEY")
    if not env.poly_wallet_address:
        missing.append("POLY_WALLET_ADDRESS")
    if not env.polygon_rpc_url:
        missing.append("POLYGON_RPC_URL")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


def _looks_like_valid_api_keys_response(payload: object) -> bool:
    if isinstance(payload, list):
        return True
    if isinstance(payload, dict):
        if "apiKeys" in payload and isinstance(payload["apiKeys"], list):
            return True
        if any(key in payload for key in ("apiKey", "apiKeys", "ids")):
            return True
    return False


if __name__ == "__main__":
    main()
