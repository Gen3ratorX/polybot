from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from aiohttp import web

from bot.config import EnvironmentConfig
from bot.control_service import ControlService
from bot.tracker import TradeTracker


def create_web_app(*, env: EnvironmentConfig, tracker: TradeTracker) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    service = ControlService(tracker, env.database_url)
    app["env"] = env
    app["tracker"] = tracker
    app["service"] = service

    app.add_routes(
        [
            web.get("/", _handle_index),
            web.get("/healthz", _handle_health),
            web.get("/api/status", _handle_status_api),
            web.post("/api/control", _handle_control_api),
            web.post("/control", _handle_control_form),
        ]
    )
    return app


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    env: EnvironmentConfig = request.app["env"]
    if not env.web_username or not env.web_password:
        return await handler(request)
    if request.path == "/healthz":
        return await handler(request)
    expected = f"{env.web_username}:{env.web_password}"
    supplied = request.headers.get("Authorization")
    if not _is_authorized(supplied, expected):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Polybot"'},
            text="Unauthorized",
        )
    return await handler(request)


def _is_authorized(header_value: str | None, expected_user_pass: str) -> bool:
    if not header_value or not header_value.startswith("Basic "):
        return False
    import base64

    token = header_value.removeprefix("Basic ").strip()
    try:
        decoded = base64.b64decode(token).decode("utf-8")
    except Exception:
        return False
    return decoded == expected_user_pass


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _handle_status_api(request: web.Request) -> web.Response:
    service: ControlService = request.app["service"]
    tracker: TradeTracker = request.app["tracker"]
    payload = _build_status_payload(service, tracker)
    return web.json_response(payload)


async def _handle_control_api(request: web.Request) -> web.Response:
    service: ControlService = request.app["service"]
    payload = await _read_json_or_form(request)
    result = _apply_control(service, payload)
    return web.json_response(result)


async def _handle_control_form(request: web.Request) -> web.Response:
    service: ControlService = request.app["service"]
    payload = await _read_json_or_form(request)
    _apply_control(service, payload)
    raise web.HTTPSeeOther("/")


async def _handle_index(request: web.Request) -> web.Response:
    service: ControlService = request.app["service"]
    tracker: TradeTracker = request.app["tracker"]
    payload = _build_status_payload(service, tracker)
    html = _render_dashboard_html(payload)
    return web.Response(text=html, content_type="text/html")


def _build_status_payload(service: ControlService, tracker: TradeTracker) -> dict[str, Any]:
    global_control = service.get_global_status()
    profiles = [service.get_profile_status(profile) for profile in service.list_profiles()]
    profile_performance = tracker.profile_performance_report()
    recent_trades = tracker.list_recent_trades(limit=10)
    open_positions = tracker.list_open_positions(limit=10)
    open_orders = tracker.list_open_orders(limit=10)
    latest_state = tracker.get_latest_state()
    return {
        "global_control": global_control,
        "profiles": [_serialize_profile_status(item) for item in profiles],
        "profile_performance": profile_performance,
        "recent_trades": [_serialize_trade(trade) for trade in recent_trades],
        "open_positions": [_serialize_position(position) for position in open_positions],
        "open_orders": [_serialize_order(order) for order in open_orders],
        "latest_state": None if latest_state is None else _serialize_state(latest_state),
    }


def _serialize_profile_status(profile: dict[str, Any]) -> dict[str, Any]:
    latest_state = profile.get("latest_state")
    return {
        **profile,
        "latest_state": None if latest_state is None else _serialize_state(latest_state),
    }


def _apply_control(service: ControlService, payload: dict[str, Any]) -> dict[str, Any]:
    command = str(payload.get("command", "")).strip().lower()
    profile_name = payload.get("profile_name")
    profile_name = None if profile_name in (None, "", "global") else str(profile_name)
    actor = str(payload.get("actor") or "web")
    if profile_name is None:
        result = service.apply_global_command(
            command,
            actor=actor,
            notes="Web control",
        )
    else:
        result = service.apply_profile_command(
            command,
            profile_name,
            actor=actor,
            notes="Web control",
        )
    return {
        "handled": result.handled,
        "command": result.command,
        "scope": result.scope,
        "profile_name": result.profile_name,
        "response": result.response,
    }


async def _read_json_or_form(request: web.Request) -> dict[str, Any]:
    if request.content_type.startswith("application/json"):
        data = await request.json()
        if isinstance(data, dict):
            return data
        return {}
    data = await request.post()
    return dict(data)


def _render_dashboard_html(payload: dict[str, Any]) -> str:
    global_control = payload["global_control"]
    profiles = payload["profiles"]
    performance = payload["profile_performance"]
    recent_trades = payload["recent_trades"]
    open_positions = payload["open_positions"]
    open_orders = payload["open_orders"]
    latest_state = payload["latest_state"]
    parts = [
        "<!doctype html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<meta http-equiv='refresh' content='20'>",
        "<title>Polybot Control</title>",
        _style_block(),
        "</head>",
        "<body>",
        "<main class='shell'>",
        "<header class='hero'>",
        "<div>",
        "<p class='eyebrow'>Polybot Control</p>",
        "<h1>Live trading control and monitoring</h1>",
        "<p class='lede'>Monitor the daemon, profiles, and control plane from one place.</p>",
        "</div>",
        "<div class='hero-card'>",
        f"<div class='metric-label'>Global state</div><div class='metric-value'>{escape(str(global_control['desired_state']))}</div>",
        f"<div class='metric-sub'>Run-once queue: {global_control['run_once_pending']}</div>",
        "</div>",
        "</header>",
        "<section class='controls'>",
        "<div class='section-head'><h2>Global Controls</h2></div>",
        _control_row_html(command="status", profile_name=None, label="Refresh"),
        _control_row_html(command="pause", profile_name=None, label="Pause All"),
        _control_row_html(command="resume", profile_name=None, label="Resume All"),
        _control_row_html(command="stop_all", profile_name=None, label="Stop All"),
        "</section>",
        "<section class='profiles'>",
        "<div class='section-head'><h2>Profiles</h2></div>",
        "<div class='profile-grid'>",
    ]
    for profile in profiles:
        parts.extend(_profile_card_html(profile))
    parts.extend(
        [
            "</div>",
            "</section>",
            "<section class='tables'>",
            "<div class='section-head'><h2>Profile Performance</h2></div>",
            _table_html(
                headers=("Profile", "Trades", "Open Orders", "Open Positions", "Win Rate", "PnL"),
                rows=[
                    (
                        item["strategy_name"],
                        item["trade_count"],
                        item["open_orders"],
                        item["open_positions"],
                        "n/a" if item["win_rate"] is None else f"{float(item['win_rate']):.1%}",
                        f"${float(item['total_pnl']):.2f}",
                    )
                    for item in performance
                ],
            ),
            "<div class='section-head'><h2>Latest State</h2></div>",
            _table_html(
                headers=("Field", "Value"),
                rows=[] if latest_state is None else [
                    ("strategy_name", latest_state["strategy_name"] or "unassigned"),
                    ("bankroll", f"${float(latest_state['bankroll']):.2f}"),
                    ("phase", latest_state["phase"]),
                    ("open_orders", latest_state["open_orders"]),
                    ("open_positions", latest_state["open_positions"]),
                    ("paused", "yes" if latest_state["is_paused"] else "no"),
                    ("pause_reason", latest_state["pause_reason"] or "n/a"),
                ],
            ),
            "<div class='section-head'><h2>Open Orders</h2></div>",
            _table_html(
                headers=("Strategy", "Market", "Status", "Requested", "Filled", "Last Seen"),
                rows=[
                    (
                        order["strategy_name"] or "unassigned",
                        order["market_id"],
                        order["status"],
                        f"{float(order['requested_size']):.2f}",
                        f"{float(order['filled_size']):.2f}",
                        order["last_seen_status"] or "n/a",
                    )
                    for order in open_orders
                ],
            ),
            "<div class='section-head'><h2>Open Positions</h2></div>",
            _table_html(
                headers=("Strategy", "Market", "Side", "Shares", "Cost Basis"),
                rows=[
                    (
                        position["strategy_name"] or "unassigned",
                        position["market_id"],
                        position["side"],
                        f"{float(position['shares']):.4f}",
                        f"${float(position['cost_basis']):.2f}",
                    )
                    for position in open_positions
                ],
            ),
            "<div class='section-head'><h2>Recent Trades</h2></div>",
            _table_html(
                headers=("Strategy", "Market", "Side", "Entry", "Fill", "PnL"),
                rows=[
                    (
                        trade["strategy_name"] or "unassigned",
                        trade["market_question"],
                        trade["side"],
                        f"{float(trade['entry_price']):.3f}",
                        "n/a" if trade["fill_price"] is None else f"{float(trade['fill_price']):.3f}",
                        "n/a" if trade["pnl"] is None else f"${float(trade['pnl']):.2f}",
                    )
                    for trade in recent_trades
                ],
            ),
            "</section>",
            "</main>",
            "</body></html>",
        ]
    )
    return "".join(parts)


def _serialize_state(state: Any) -> dict[str, Any]:
    return {
        "timestamp": _serialize_datetime(state.timestamp),
        "strategy_name": state.strategy_name,
        "bankroll": state.bankroll,
        "phase": state.phase,
        "bankroll_start_of_day": state.bankroll_start_of_day,
        "bankroll_start_of_week": state.bankroll_start_of_week,
        "daily_pnl": state.daily_pnl,
        "weekly_pnl": state.weekly_pnl,
        "total_trades": state.total_trades,
        "consecutive_failures": state.consecutive_failures,
        "open_orders": state.open_orders,
        "open_positions": state.open_positions,
        "strategy_min_price": state.strategy_min_price,
        "strategy_min_score": state.strategy_min_score,
        "is_paused": state.is_paused,
        "pause_level": state.pause_level,
        "pause_reason": state.pause_reason,
        "pause_until": _serialize_datetime(state.pause_until),
        "recent_trades": [_serialize_trade(trade) for trade in state.recent_trades],
    }


def _serialize_trade(trade: Any) -> dict[str, Any]:
    return {
        "timestamp": _serialize_datetime(trade.timestamp),
        "strategy_name": trade.strategy_name,
        "market_id": trade.market_id,
        "market_question": trade.market_question,
        "category": trade.category,
        "side": trade.side.value if hasattr(trade.side, "value") else str(trade.side),
        "entry_price": trade.entry_price,
        "screened_price": trade.screened_price,
        "position_size": trade.position_size,
        "score": trade.score,
        "hours_to_close": trade.hours_to_close,
        "order_id": trade.order_id,
        "fill_price": trade.fill_price,
        "fill_time": _serialize_datetime(trade.fill_time),
        "fill_slippage": trade.fill_slippage,
        "fill_slippage_pct": trade.fill_slippage_pct,
        "outcome": trade.outcome.value if hasattr(trade.outcome, "value") else str(trade.outcome),
        "resolution_price": trade.resolution_price,
        "pnl": trade.pnl,
        "execution_fee": trade.execution_fee,
        "paper_trade": trade.paper_trade,
    }


def _serialize_position(position: Any) -> dict[str, Any]:
    return {
        "timestamp": _serialize_datetime(position.timestamp),
        "strategy_name": position.strategy_name,
        "market_id": position.market_id,
        "market_question": position.market_question,
        "category": position.category,
        "side": position.side.value if hasattr(position.side, "value") else str(position.side),
        "fill_price": position.fill_price,
        "shares": position.shares,
        "cost_basis": position.cost_basis,
        "position_id": position.position_id,
        "order_id": position.order_id,
        "status": position.status.value if hasattr(position.status, "value") else str(position.status),
        "resolved_at": _serialize_datetime(position.resolved_at),
        "resolution_price": position.resolution_price,
        "pnl": position.pnl,
        "paper_trade": position.paper_trade,
    }


def _serialize_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order["id"],
        "timestamp": _serialize_datetime(order["timestamp"]),
        "order_id": order["order_id"],
        "strategy_name": order["strategy_name"],
        "market_id": order["market_id"],
        "status": order["status"],
        "requested_size": order["requested_size"],
        "limit_price": order["limit_price"],
        "filled_size": order["filled_size"],
        "last_seen_status": order["last_seen_status"],
        "last_seen_at": _serialize_datetime(order["last_seen_at"]),
        "exchange_payload": order["exchange_payload"],
    }


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _profile_card_html(profile: dict[str, Any]) -> list[str]:
    profile_name = profile["profile_name"]
    return [
        "<article class='card'>",
        f"<div class='card-title'>{escape(str(profile_name))}</div>",
        "<div class='card-grid'>",
        _stat_cell("Effective", profile["effective_state"]),
        _stat_cell("Profile", profile["profile_state"]),
        _stat_cell("Global", profile["global_state"]),
        _stat_cell("Run once", profile["run_once_pending"]),
        _stat_cell("Bankroll", f"${float(profile['bankroll']):.2f}"),
        _stat_cell("Open orders", profile["open_orders"]),
        _stat_cell("Open positions", profile["open_positions"]),
        _stat_cell("Trades", profile["trade_count"]),
        _stat_cell("Win rate", "n/a" if profile["win_rate"] is None else f"{float(profile['win_rate']):.1%}"),
        _stat_cell("PnL", f"${float(profile['total_pnl']):.2f}"),
        "</div>",
        "<div class='button-row'>",
        _button_form("status", profile_name, "Status"),
        _button_form("pause", profile_name, "Pause"),
        _button_form("resume", profile_name, "Resume"),
        _button_form("run_once", profile_name, "Run Once"),
        _button_form("start", profile_name, "Start"),
        _button_form("stop", profile_name, "Stop"),
        "</div>",
        "</article>",
    ]


def _button_form(command: str, profile_name: str | None, label: str) -> str:
    profile_value = "" if profile_name is None else escape(profile_name)
    return (
        "<form method='post' action='/control' class='inline-form'>"
        f"<input type='hidden' name='command' value='{escape(command)}'>"
        f"<input type='hidden' name='profile_name' value='{profile_value}'>"
        f"<button type='submit'>{escape(label)}</button>"
        "</form>"
    )


def _control_row_html(command: str, profile_name: str | None, label: str) -> str:
    return (
        "<form method='post' action='/control' class='control-row'>"
        f"<input type='hidden' name='command' value='{escape(command)}'>"
        f"<input type='hidden' name='profile_name' value='{'' if profile_name is None else escape(profile_name)}'>"
        f"<button type='submit'>{escape(label)}</button>"
        "</form>"
    )


def _stat_cell(label: str, value: Any) -> str:
    return (
        "<div class='stat'>"
        f"<div class='stat-label'>{escape(str(label))}</div>"
        f"<div class='stat-value'>{escape(str(value))}</div>"
        "</div>"
    )


def _table_html(*, headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    cells = ["<table>", "<thead><tr>"]
    for header in headers:
        cells.append(f"<th>{escape(str(header))}</th>")
    cells.append("</tr></thead><tbody>")
    if not rows:
        cells.append(f"<tr><td colspan='{len(headers)}' class='muted'>No records</td></tr>")
    else:
        for row in rows:
            cells.append("<tr>")
            for cell in row:
                cells.append(f"<td>{escape(str(cell))}</td>")
            cells.append("</tr>")
    cells.append("</tbody></table>")
    return "".join(cells)


def _style_block() -> str:
    return """
    <style>
      :root {
        color-scheme: dark;
        --bg: #0b1020;
        --panel: #121a2d;
        --panel-2: #16213a;
        --border: rgba(255,255,255,0.08);
        --text: #e8ecf5;
        --muted: #96a3bd;
        --accent: #55d6be;
        --accent-2: #75a7ff;
        --danger: #ff6b6b;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        background:
          radial-gradient(circle at top left, rgba(85,214,190,0.14), transparent 28%),
          radial-gradient(circle at top right, rgba(117,167,255,0.12), transparent 24%),
          var(--bg);
        color: var(--text);
      }
      .shell {
        max-width: 1440px;
        margin: 0 auto;
        padding: 32px 24px 48px;
      }
      .hero {
        display: grid;
        grid-template-columns: 1.5fr 0.7fr;
        gap: 20px;
        align-items: stretch;
        margin-bottom: 24px;
      }
      .hero h1 { margin: 4px 0 10px; font-size: clamp(2rem, 3vw, 3.5rem); line-height: 1.02; }
      .eyebrow { margin: 0; text-transform: uppercase; letter-spacing: 0.18em; color: var(--accent); font-size: 0.75rem; }
      .lede { margin: 0; color: var(--muted); max-width: 62ch; }
      .hero-card, .card, table, .control-row {
        background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.018));
        border: 1px solid var(--border);
        border-radius: 18px;
        box-shadow: 0 20px 50px rgba(0,0,0,0.25);
      }
      .hero-card {
        padding: 22px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        min-height: 160px;
      }
      .metric-label, .stat-label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.12em; }
      .metric-value { font-size: 2.2rem; font-weight: 700; margin: 6px 0; }
      .metric-sub { color: var(--muted); }
      .section-head { display: flex; align-items: center; justify-content: space-between; margin: 18px 0 10px; }
      .section-head h2 { margin: 0; font-size: 1.2rem; }
      .controls, .profiles, .tables { margin-bottom: 28px; }
      .control-row {
        display: inline-flex;
        margin: 0 10px 10px 0;
        overflow: hidden;
      }
      .control-row button, .inline-form button {
        background: transparent;
        color: var(--text);
        border: 0;
        padding: 12px 16px;
        cursor: pointer;
        font-weight: 600;
      }
      .profile-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
        gap: 18px;
      }
      .card { padding: 18px; }
      .card-title { font-size: 1.05rem; font-weight: 700; margin-bottom: 14px; }
      .card-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        margin-bottom: 14px;
      }
      .stat {
        padding: 12px;
        border-radius: 14px;
        background: rgba(255,255,255,0.025);
        border: 1px solid rgba(255,255,255,0.04);
      }
      .stat-value { margin-top: 4px; font-size: 1rem; font-weight: 600; word-break: break-word; }
      .button-row { display: flex; flex-wrap: wrap; gap: 8px; }
      .inline-form, .button-row form { margin: 0; }
      table { width: 100%; border-collapse: collapse; overflow: hidden; }
      th, td { padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.06); text-align: left; }
      th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em; }
      .muted { color: var(--muted); }
      .profile-grid .card:last-child { margin-bottom: 0; }
      @media (max-width: 900px) {
        .hero { grid-template-columns: 1fr; }
        .card-grid { grid-template-columns: 1fr; }
      }
    </style>
    """
