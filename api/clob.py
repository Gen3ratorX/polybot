from __future__ import annotations

from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

from bot.config import EnvironmentConfig


@dataclass(frozen=True, slots=True)
class CollateralStatus:
    balance_raw: str
    balance_usdc: float
    allowances_raw: dict[str, str]
    allowances_usdc: dict[str, float]


def api_creds_from_env(env: EnvironmentConfig) -> ApiCreds | None:
    if not (env.poly_api_key and env.poly_api_secret and env.poly_api_passphrase):
        return None
    return ApiCreds(
        api_key=env.poly_api_key,
        api_secret=env.poly_api_secret,
        api_passphrase=env.poly_api_passphrase,
    )


def build_clob_client(
    env: EnvironmentConfig,
    *,
    include_api_creds: bool = True,
) -> ClobClient:
    creds = api_creds_from_env(env) if include_api_creds else None
    return ClobClient(
        host=env.poly_clob_host,
        chain_id=env.poly_chain_id,
        key=env.poly_private_key,
        creds=creds,
        signature_type=env.poly_signature_type,
        funder=env.poly_wallet_address,
    )


def collateral_balance_params(env: EnvironmentConfig) -> BalanceAllowanceParams:
    return BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=env.poly_signature_type,
    )


def get_collateral_status(client: ClobClient, env: EnvironmentConfig) -> CollateralStatus:
    payload = client.get_balance_allowance(collateral_balance_params(env))
    return parse_collateral_status(payload)


def parse_collateral_status(payload: dict[str, object]) -> CollateralStatus:
    balance_raw = str(payload.get("balance", "0"))
    allowances_obj = payload.get("allowances") or {}
    if not isinstance(allowances_obj, dict):
        raise ValueError("allowances payload must be a dictionary")

    allowances_raw = {str(key): str(value) for key, value in allowances_obj.items()}
    allowances_usdc = {
        address: raw_usdc_to_float(raw_value)
        for address, raw_value in allowances_raw.items()
    }

    return CollateralStatus(
        balance_raw=balance_raw,
        balance_usdc=raw_usdc_to_float(balance_raw),
        allowances_raw=allowances_raw,
        allowances_usdc=allowances_usdc,
    )


def raw_usdc_to_float(raw_value: str | int | float) -> float:
    return float(raw_value) / 1_000_000
