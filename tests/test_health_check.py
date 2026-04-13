from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.config import load_environment
from scripts.health_check import _looks_like_valid_api_keys_response, ensure_required_env


def test_health_check_requires_private_key_wallet_and_rpc(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POLYGON_RPC_URL=https://polygon-rpc.com\n")

    previous = dict(os.environ)
    try:
        for key in ["POLY_PRIVATE_KEY", "POLY_WALLET_ADDRESS", "POLYGON_RPC_URL"]:
            os.environ.pop(key, None)
        env = load_environment(env_file)
    finally:
        os.environ.clear()
        os.environ.update(previous)

    with pytest.raises(ValueError, match="POLY_PRIVATE_KEY, POLY_WALLET_ADDRESS"):
        ensure_required_env(env)


def test_health_check_accepts_dict_style_api_key_responses() -> None:
    assert _looks_like_valid_api_keys_response([{"apiKey": "x"}]) is True
    assert _looks_like_valid_api_keys_response({"apiKeys": [{"apiKey": "x"}]}) is True
    assert _looks_like_valid_api_keys_response({"apiKey": "x"}) is True
    assert _looks_like_valid_api_keys_response({"unexpected": "value"}) is False
