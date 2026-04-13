from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.clob import build_clob_client
from bot.config import load_environment


def main() -> None:
    env = load_environment()

    missing: list[str] = []
    if not env.poly_private_key:
        missing.append("POLY_PRIVATE_KEY")
    if not env.poly_wallet_address:
        missing.append("POLY_WALLET_ADDRESS")
    if missing:
        print(
            f"derive_api_creds_error: Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client = build_clob_client(env, include_api_creds=False)
    creds = client.create_or_derive_api_creds()
    if creds is None:
        print("derive_api_creds_error: Could not create or derive API credentials.", file=sys.stderr)
        raise SystemExit(1)

    print(
        json.dumps(
            {
                "POLY_API_KEY": creds.api_key,
                "POLY_API_SECRET": creds.api_secret,
                "POLY_API_PASSPHRASE": creds.api_passphrase,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
