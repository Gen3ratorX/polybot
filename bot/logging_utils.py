from __future__ import annotations

import logging


def configure_cli_logging(*, debug_http: bool = False) -> None:
    level = logging.DEBUG if debug_http else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if debug_http:
        for logger_name in ("api.gamma", "api.spot", "api.catalyst", "aiohttp.client", "aiohttp.access"):
            logging.getLogger(logger_name).setLevel(logging.DEBUG)
