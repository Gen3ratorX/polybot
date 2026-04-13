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
    recent_trades = tracker.list_recent_trades(limit=20)
    open_positions = tracker.list_open_positions(limit=20)
    open_orders = tracker.list_open_orders(limit=20)
    latest_state = tracker.get_latest_state()
    global_curve_svg = _render_curve_svg(
        _cumulative_pnl_points(recent_trades),
        title="Recent Equity Curve",
        subtitle="Resolved trades from the latest ledger sample.",
        accent="#7aa7ff",
    )
    return {
        "global_control": global_control,
        "profiles": [_serialize_profile_status(item) for item in profiles],
        "profile_performance": profile_performance,
        "recent_trades": [_serialize_trade(trade) for trade in recent_trades],
        "open_positions": [_serialize_position(position) for position in open_positions],
        "open_orders": [_serialize_order(order) for order in open_orders],
        "latest_state": None if latest_state is None else _serialize_state(latest_state),
        "global_curve_svg": global_curve_svg,
    }


def _serialize_profile_status(profile: dict[str, Any]) -> dict[str, Any]:
    latest_state = profile.get("latest_state")
    equity_curve_svg = None
    if latest_state is not None and getattr(latest_state, "recent_trades", None):
        equity_curve_svg = _render_curve_svg(
            _cumulative_pnl_points(list(latest_state.recent_trades)),
            title=f"{profile['profile_name']} Equity Curve",
            subtitle="Resolved trade PnL from recent profile history.",
            accent="#63e6be" if profile.get("profile_state") != "STOPPED" else "#ffcd70",
        )
    return {
        **profile,
        "latest_state": None if latest_state is None else _serialize_state(latest_state),
        "equity_curve_svg": equity_curve_svg,
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
    global_curve_svg = payload.get("global_curve_svg")
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
        "<header class='topbar'>",
        "<div class='brand-lockup'>",
        "<div class='brand-mark'>P</div>",
        "<div>",
        "<p class='eyebrow'>Polybot Control Plane</p>",
        "<div class='brand-row'>",
        "<h1>Dashboard</h1>",
        "<span class='live-pill'>LIVE</span>",
        "</div>",
        "</div>",
        "</div>",
        "<div class='topbar-meta'>",
        "<div class='topbar-item'><span>Global</span><strong>{}</strong></div>".format(escape(str(global_control["desired_state"]))),
        "<div class='topbar-item'><span>Run-once queue</span><strong>{}</strong></div>".format(global_control["run_once_pending"]),
        "</div>",
        "</header>",
        "<header class='hero'>",
        "<div>",
        "<p class='lede'>Monitor the daemon, profiles, and control plane from one place. Everything is read from the live SQLite ledger and rendered with the current control state.</p>",
        "<div class='hero-chips'>",
        _chip(f"Bankroll ${float(latest_state['bankroll']):.2f}" if latest_state else "Bankroll n/a"),
        _chip(f"Trades {sum(item['trade_count'] for item in performance)}"),
        _chip(f"Open positions {sum(item['open_positions'] for item in performance)}"),
        _chip(f"Open orders {sum(item['open_orders'] for item in performance)}"),
        "</div>",
        "</div>",
        "<div class='hero-card hero-card--status'>",
        "<div class='metric-label'>System state</div>",
        f"<div class='metric-value'>{escape(str(global_control['desired_state']))}</div>",
        f"<div class='metric-sub'>Run-once queue: {global_control['run_once_pending']}</div>",
        "<div class='status-grid'>",
        f"<div><span>Profiles</span><strong>{len(profiles)}</strong></div>",
        f"<div><span>Open positions</span><strong>{sum(item['open_positions'] for item in performance)}</strong></div>",
        f"<div><span>Open orders</span><strong>{sum(item['open_orders'] for item in performance)}</strong></div>",
        f"<div><span>Resolved trades</span><strong>{sum(item['trade_count'] for item in performance)}</strong></div>",
        "</div>",
        "</div>",
        "</header>",
        "<section class='chart-row'>",
        "<div class='section-head'>",
        "<div>",
        "<h2>Market Tape</h2>",
        "<p>Live equity and execution quality, rendered from the ledger.</p>",
        "</div>",
        "</div>",
        f"<div class='chart-frame'>{global_curve_svg}</div>" if global_curve_svg else "<div class='chart-frame empty'>No chart data yet</div>",
        "</section>",
        "<section class='controls'>",
        "<div class='section-head'>",
        "<div>",
        "<h2>Global Controls</h2>",
        "<p>Apply a command to both strategies at once.</p>",
        "</div>",
        "</div>",
        _control_row_html(command="status", profile_name=None, label="Refresh"),
        _control_row_html(command="pause", profile_name=None, label="Pause All"),
        _control_row_html(command="resume", profile_name=None, label="Resume All"),
        _control_row_html(command="stop_all", profile_name=None, label="Stop All"),
        "</section>",
        "<section class='profiles'>",
        "<div class='section-head'>",
        "<div>",
        "<h2>Profiles</h2>",
        "<p>Profile-level control with separate accounting and live state.</p>",
        "</div>",
        "</div>",
        "<div class='profile-grid'>",
    ]
    for profile in profiles:
        parts.extend(_profile_card_html(profile))
    parts.extend(
        [
            "</div>",
            "</section>",
            "<section class='tables'>",
            "<div class='section-head'>",
            "<div>",
            "<h2>Profile Performance</h2>",
            "<p>Performance is split per strategy so one profile cannot hide the other.</p>",
            "</div>",
            "</div>",
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
            "<div class='section-head'>",
            "<div>",
            "<h2>Latest State</h2>",
            "<p>Snapshot from the most recent ledger state.</p>",
            "</div>",
            "</div>",
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


def _cumulative_pnl_points(trades: list[Any] | tuple[Any, ...]) -> list[float]:
    cumulative = 0.0
    points: list[float] = []
    for trade in trades:
        pnl = getattr(trade, "pnl", None)
        if pnl is None:
            continue
        cumulative += float(pnl)
        points.append(round(cumulative, 6))
    if not points:
        return [0.0, 0.0]
    if len(points) == 1:
        points = [0.0, points[0]]
    return points


def _render_curve_svg(
    values: list[float],
    *,
    title: str,
    subtitle: str,
    accent: str,
) -> str:
    width = 880
    height = 220
    padding_x = 26
    padding_y = 24
    plot_width = width - (padding_x * 2)
    plot_height = height - (padding_y * 2)
    minimum = min(values)
    maximum = max(values)
    if abs(maximum - minimum) < 1e-9:
        maximum = minimum + 1.0
    points: list[str] = []
    for index, value in enumerate(values):
        x = padding_x + (plot_width * (index / max(1, len(values) - 1)))
        y_ratio = (value - minimum) / (maximum - minimum)
        y = padding_y + plot_height - (plot_height * y_ratio)
        points.append(f"{x:.1f},{y:.1f}")
    path = " ".join(points)
    chart_id = f"grad-{abs(hash(title)) % 100000}"
    min_label = f"${minimum:+.2f}"
    max_label = f"${maximum:+.2f}"
    current_label = f"${values[-1]:+.2f}"
    return (
        f"<svg viewBox='0 0 880 220' role='img' aria-label='{escape(title)}' class='curve-svg'>"
        "<defs>"
        f"<linearGradient id='{chart_id}' x1='0' x2='0' y1='0' y2='1'>"
        f"<stop offset='0%' stop-color='{escape(accent)}' stop-opacity='0.92'/>"
        f"<stop offset='100%' stop-color='{escape(accent)}' stop-opacity='0.05'/>"
        f"</linearGradient>"
        "</defs>"
        f"<rect x='0' y='0' width='880' height='220' rx='18' fill='rgba(255,255,255,0.02)' stroke='rgba(255,255,255,0.07)'/>"
        f"<text x='24' y='34' class='chart-title'>{escape(title)}</text>"
        f"<text x='24' y='56' class='chart-subtitle'>{escape(subtitle)}</text>"
        f"<text x='836' y='34' text-anchor='end' class='chart-pill'>{escape(current_label)}</text>"
        "<line x1='24' y1='180' x2='856' y2='180' stroke='rgba(255,255,255,0.08)'/>"
        "<line x1='24' y1='128' x2='856' y2='128' stroke='rgba(255,255,255,0.05)'/>"
        "<line x1='24' y1='76' x2='856' y2='76' stroke='rgba(255,255,255,0.05)'/>"
        f"<path d='M {path}' fill='none' stroke='{escape(accent)}' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/>"
        f"<path d='M 26,180 {path} 854,180 Z' fill='url(#{chart_id})' opacity='0.9'/>"
        f"<text x='24' y='198' class='chart-axis'>{escape(min_label)}</text>"
        f"<text x='836' y='198' text-anchor='end' class='chart-axis'>{escape(max_label)}</text>"
        "</svg>"
    )


def _profile_card_html(profile: dict[str, Any]) -> list[str]:
    profile_name = profile["profile_name"]
    effective = str(profile["effective_state"])
    profile_state = str(profile["profile_state"])
    global_state = str(profile["global_state"])
    equity_curve_svg = profile.get("equity_curve_svg")
    return [
        "<article class='card'>",
        "<div class='card-head'>",
        f"<div class='card-title'>{escape(str(profile_name))}</div>",
        f"<div class='card-badges'>{_state_badge(effective)} {_state_badge(profile_state, accent='secondary')} {_state_badge(global_state, accent='muted')}</div>",
        "</div>",
        f"<div class='card-chart'>{equity_curve_svg}</div>" if equity_curve_svg else "<div class='card-chart empty'>No equity curve yet</div>",
        "<div class='card-grid'>",
        _stat_cell("Effective", effective),
        _stat_cell("Profile", profile_state),
        _stat_cell("Global", global_state),
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


def _chip(text: str) -> str:
    return f"<span class='chip'>{escape(text)}</span>"


def _state_badge(value: str, *, accent: str = "primary") -> str:
    normalized = value.strip().upper()
    return f"<span class='badge badge--{escape(accent)} badge--{escape(normalized.lower())}'>{escape(normalized)}</span>"


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
        --bg: #08111f;
        --panel: rgba(12, 18, 33, 0.8);
        --panel-2: rgba(18, 25, 43, 0.8);
        --border: rgba(255,255,255,0.08);
        --border-strong: rgba(255,255,255,0.12);
        --text: #edf2ff;
        --muted: #9aabc7;
        --accent: #63e6be;
        --accent-2: #7aa7ff;
        --danger: #ff6b6b;
        --warning: #ffcd70;
        --shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      }
      * { box-sizing: border-box; }
      html {
        scroll-behavior: smooth;
      }
      body {
        margin: 0;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
        background:
          radial-gradient(circle at top left, rgba(99,230,190,0.14), transparent 25%),
          radial-gradient(circle at top right, rgba(122,167,255,0.12), transparent 22%),
          radial-gradient(circle at bottom center, rgba(255,255,255,0.05), transparent 35%),
          var(--bg);
        color: var(--text);
        min-height: 100vh;
      }
      .shell {
        max-width: 1500px;
        margin: 0 auto;
        padding: 28px 24px 56px;
      }
      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        padding: 18px 20px;
        margin-bottom: 18px;
        border: 1px solid var(--border);
        border-radius: 20px;
        background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02));
        box-shadow: var(--shadow);
        backdrop-filter: blur(18px);
      }
      .brand-lockup {
        display: flex;
        align-items: center;
        gap: 16px;
      }
      .brand-mark {
        width: 48px;
        height: 48px;
        display: grid;
        place-items: center;
        border-radius: 16px;
        background: linear-gradient(135deg, rgba(99,230,190,0.9), rgba(122,167,255,0.85));
        color: #04111f;
        font-weight: 900;
        font-size: 1.1rem;
        box-shadow: 0 10px 30px rgba(99,230,190,0.2);
      }
      .brand-row {
        display: flex;
        align-items: center;
        gap: 12px;
      }
      .brand-row h1 {
        margin: 0;
        font-size: 1.7rem;
        line-height: 1;
      }
      .topbar-meta {
        display: flex;
        align-items: stretch;
        gap: 12px;
        flex-wrap: wrap;
      }
      .topbar-item {
        min-width: 132px;
        padding: 10px 14px;
        border-radius: 14px;
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.05);
      }
      .topbar-item span {
        display: block;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        margin-bottom: 4px;
      }
      .topbar-item strong {
        font-size: 1.02rem;
        font-variant-numeric: tabular-nums;
      }
      .live-pill, .badge, .chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .live-pill {
        padding: 7px 12px;
        background: rgba(99,230,190,0.12);
        color: var(--accent);
        border: 1px solid rgba(99,230,190,0.24);
      }
      .badge {
        padding: 5px 10px;
        border: 1px solid transparent;
      }
      .badge--primary {
        background: rgba(122,167,255,0.12);
        color: #bfd0ff;
        border-color: rgba(122,167,255,0.2);
      }
      .badge--secondary {
        background: rgba(99,230,190,0.1);
        color: #bdf6e8;
        border-color: rgba(99,230,190,0.18);
      }
      .badge--muted {
        background: rgba(255,255,255,0.05);
        color: var(--muted);
        border-color: rgba(255,255,255,0.08);
      }
      .badge--running { background: rgba(99,230,190,0.12); color: var(--accent); }
      .badge--paused { background: rgba(255,205,112,0.12); color: var(--warning); }
      .badge--stopped { background: rgba(255,107,107,0.12); color: var(--danger); }
      .chip {
        padding: 8px 12px;
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.06);
        color: var(--text);
      }
      .hero {
        display: grid;
        grid-template-columns: 1.4fr 0.82fr;
        gap: 20px;
        align-items: stretch;
        margin-bottom: 24px;
      }
      .hero h1 { margin: 0; font-size: clamp(2.2rem, 4vw, 4rem); line-height: 0.96; }
      .eyebrow { margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--accent); font-size: 0.75rem; }
      .lede { margin: 0; color: var(--muted); max-width: 62ch; font-size: 1.02rem; line-height: 1.6; }
      .hero-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 16px;
      }
      .chart-row {
        margin-bottom: 24px;
      }
      .chart-frame {
        padding: 16px;
        border-radius: 22px;
        background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.02));
        border: 1px solid var(--border);
        box-shadow: var(--shadow);
        backdrop-filter: blur(14px);
      }
      .card-chart {
        padding: 12px;
        margin-bottom: 14px;
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.018));
        border: 1px solid rgba(255,255,255,0.06);
      }
      .chart-frame.empty, .card-chart.empty {
        min-height: 220px;
        display: grid;
        place-items: center;
        color: var(--muted);
      }
      .curve-svg {
        display: block;
        width: 100%;
        height: auto;
      }
      .chart-title {
        fill: var(--text);
        font-size: 18px;
        font-weight: 700;
        letter-spacing: -0.02em;
      }
      .chart-subtitle, .chart-axis {
        fill: var(--muted);
        font-size: 12px;
      }
      .chart-pill {
        fill: var(--text);
        font-size: 13px;
        font-weight: 700;
      }
      .hero-card, .card, table, .control-row {
        background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.02));
        border: 1px solid var(--border);
        border-radius: 18px;
        box-shadow: var(--shadow);
        backdrop-filter: blur(14px);
      }
      .hero-card {
        padding: 22px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        min-height: 220px;
      }
      .metric-label, .stat-label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.12em; }
      .metric-value { font-size: 2.5rem; font-weight: 800; margin: 8px 0 4px; letter-spacing: -0.04em; font-variant-numeric: tabular-nums; }
      .metric-sub { color: var(--muted); }
      .status-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        margin-top: 18px;
      }
      .status-grid div {
        padding: 12px 14px;
        border-radius: 14px;
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.05);
      }
      .status-grid span {
        display: block;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        margin-bottom: 6px;
      }
      .status-grid strong {
        font-size: 1.08rem;
      }
      .section-head {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        margin: 18px 0 12px;
      }
      .section-head h2 { margin: 0; font-size: 1.2rem; }
      .section-head p { margin: 6px 0 0; color: var(--muted); }
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
        grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
        gap: 18px;
      }
      .card { padding: 18px; }
      .card-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 14px;
      }
      .card-title { font-size: 1.05rem; font-weight: 800; letter-spacing: -0.02em; }
      .card-badges { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
      .card-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
        margin-bottom: 14px;
      }
      .stat {
        padding: 12px;
        border-radius: 14px;
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.05);
      }
      .stat-value { margin-top: 4px; font-size: 1rem; font-weight: 700; word-break: break-word; font-variant-numeric: tabular-nums; }
      .button-row { display: flex; flex-wrap: wrap; gap: 8px; }
      .inline-form, .button-row form { margin: 0; }
      table { width: 100%; border-collapse: collapse; overflow: hidden; }
      th, td { padding: 12px 14px; border-bottom: 1px solid rgba(255,255,255,0.06); text-align: left; }
      th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.12em; }
      .muted { color: var(--muted); }
      .profile-grid .card:last-child { margin-bottom: 0; }
      .controls .control-row button,
      .inline-form button {
        transition: transform 0.18s ease, background-color 0.18s ease, border-color 0.18s ease, opacity 0.18s ease;
      }
      .controls .control-row button:hover,
      .inline-form button:hover {
        transform: translateY(-1px);
        background: rgba(255,255,255,0.04);
      }
      .controls .control-row button:active,
      .inline-form button:active {
        transform: translateY(0);
        opacity: 0.9;
      }
      @media (max-width: 900px) {
        .hero { grid-template-columns: 1fr; }
        .card-grid { grid-template-columns: 1fr; }
        .topbar { flex-direction: column; align-items: flex-start; }
        .topbar-meta { width: 100%; }
        .chart-frame { padding: 12px; }
      }
    </style>
    """
