"""budgetctl -- The circuit breaker for AI agent spend.

Single-file CLI built with Click + Rich that talks to Chappie's FastAPI
API over HTTP.  All commands use httpx (sync) and Rich for output.

Usage:
    budgetctl status
    budgetctl budget list
    budgetctl budget set agent email-agent 50.00
    budgetctl budget get agent email-agent
    budgetctl --format json status

Environment:
    CHAPPIE_API_URL   Base URL of the Chappie API (default: http://localhost:8787)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()
error_console = Console(stderr=True)

API_URL_DEFAULT = "http://localhost:8787"
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0


# -----------------------------------------------------------------------
# HTTP helper
# -----------------------------------------------------------------------


def _client(api_url: str) -> httpx.Client:
    """Build a pre-configured httpx client."""
    return httpx.Client(
        base_url=api_url,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
                              write=READ_TIMEOUT, pool=READ_TIMEOUT),
    )


def _request(
    ctx: click.Context,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
) -> dict | list | None:
    """Execute an HTTP request and return parsed JSON.

    Handles connection errors and non-2xx responses with user-friendly
    Rich output, then exits with a non-zero code.
    """
    api_url: str = ctx.obj["api_url"]

    try:
        with _client(api_url) as client:
            response = client.request(method, path, json=json_body)
    except httpx.ConnectError:
        error_console.print(
            f"[red]Cannot connect to Chappie API at {api_url}[/red]"
        )
        error_console.print(
            "[dim]Is the Chappie server running? "
            "Start it with: uvicorn chappie.api:app --port 8787[/dim]"
        )
        sys.exit(1)
    except httpx.TimeoutException:
        error_console.print(
            f"[red]Request timed out connecting to {api_url}[/red]"
        )
        sys.exit(1)
    except httpx.HTTPError as exc:
        error_console.print(f"[red]HTTP error: {exc}[/red]")
        sys.exit(1)

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        error_console.print(
            f"[red]API error ({response.status_code}): {detail}[/red]"
        )
        sys.exit(1)

    if response.status_code == 204:
        return None

    try:
        return response.json()
    except Exception:
        return None


def _output_json(data: object) -> None:
    """Dump raw JSON to stdout for scripting."""
    console.print_json(json.dumps(data, default=str))


# -----------------------------------------------------------------------
# Display helpers
# -----------------------------------------------------------------------


def _pct_color(percentage: float) -> str:
    """Return a Rich color tag based on budget usage percentage."""
    if percentage >= 80:
        return "red"
    if percentage >= 50:
        return "yellow"
    return "green"


def _pct_status(percentage: float) -> str:
    """Return a status label based on budget usage percentage."""
    if percentage >= 100:
        return "EXCEEDED"
    if percentage >= 80:
        return "WARNING"
    return "OK"


def _format_cooldown(open_until_iso: str | None) -> str:
    """Format remaining cooldown as human-readable string."""
    if not open_until_iso:
        return ""

    try:
        # Handle both ISO format and Unix timestamp
        try:
            deadline = datetime.fromisoformat(open_until_iso)
        except (ValueError, TypeError):
            deadline = datetime.fromtimestamp(float(open_until_iso), tz=timezone.utc)

        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)

        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()

        if remaining <= 0:
            return "expired"

        minutes = int(remaining // 60)
        seconds = int(remaining % 60)

        if minutes > 0:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"
    except (ValueError, TypeError, OSError):
        return ""


def _cb_state_styled(state: str) -> Text:
    """Return a Rich Text object with the CB state colored."""
    state_upper = state.upper()
    color_map = {
        "OPEN": "red",
        "HALF_OPEN": "yellow",
        "CLOSED": "green",
    }
    color = color_map.get(state_upper, "white")
    return Text(state_upper, style=color)


# -----------------------------------------------------------------------
# CLI root group
# -----------------------------------------------------------------------


@click.group()
@click.option(
    "--api-url",
    envvar="CHAPPIE_API_URL",
    default=API_URL_DEFAULT,
    show_default=True,
    help="Base URL of the Chappie API.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format (table for humans, json for scripts).",
)
@click.pass_context
def cli(ctx: click.Context, api_url: str, output_format: str) -> None:
    """budgetctl -- The circuit breaker for AI agent spend."""
    ctx.ensure_object(dict)
    ctx.obj["api_url"] = api_url.rstrip("/")
    ctx.obj["format"] = output_format


# -----------------------------------------------------------------------
# status command
# -----------------------------------------------------------------------


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show system overview and active circuit breakers."""
    data = _request(ctx, "GET", "/api/status")

    if ctx.obj["format"] == "json":
        _output_json(data)
        return

    # -- Header panel --
    mode = data.get("mode", "unknown")
    store_type = data.get("store", "unknown")
    store_connected = data.get("store_connected", False)
    store_label = f"{store_type} ({'connected' if store_connected else 'disconnected'})"
    agents_tracked = data.get("agents_tracked", 0)
    total_spend = data.get("total_spend", 0.0)
    loops_caught = data.get("loops_caught", 0)
    cb_tripped = data.get("cb_tripped", 0)

    lines = [
        f"[bold]Mode:[/bold]           {mode}",
        f"[bold]Store:[/bold]          {store_label}",
        f"[bold]Agents tracked:[/bold] {agents_tracked}",
        f"[bold]Total spend:[/bold]    ${total_spend:.2f}",
        f"[bold]Loops caught:[/bold]   {loops_caught}",
        f"[bold]CB tripped:[/bold]     {cb_tripped}",
    ]

    panel = Panel(
        "\n".join(lines),
        title="[bold cyan]Chappie Status[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)

    # -- Active circuit breakers table --
    breakers = data.get("circuit_breakers", [])
    active = [
        b for b in breakers
        if b.get("state", "closed").lower() != "closed"
    ]

    if not active:
        console.print("[dim]No active circuit breakers.[/dim]")
        return

    table = Table(
        title="Active Circuit Breakers",
        title_style="bold",
        show_lines=False,
        padding=(0, 2),
    )
    table.add_column("Agent", style="bold")
    table.add_column("State")
    table.add_column("Reason")
    table.add_column("Cooldown", justify="right")

    for b in active:
        state = b.get("state", "unknown")
        reason = b.get("reason", "")
        open_until = b.get("open_until")

        if state.lower() == "half_open":
            cooldown_display = "probing..."
        else:
            cooldown_display = _format_cooldown(open_until)

        table.add_row(
            b.get("agent_id", "unknown"),
            _cb_state_styled(state),
            reason,
            cooldown_display,
        )

    console.print()
    console.print(table)


# -----------------------------------------------------------------------
# budget command group
# -----------------------------------------------------------------------


@cli.group()
@click.pass_context
def budget(ctx: click.Context) -> None:
    """Manage agent and user budgets."""
    pass


@budget.command("list")
@click.pass_context
def budget_list(ctx: click.Context) -> None:
    """List all budgets with spend and limit info."""
    data = _request(ctx, "GET", "/api/budgets")

    if ctx.obj["format"] == "json":
        _output_json(data)
        return

    budgets = data if isinstance(data, list) else data.get("budgets", [])

    if not budgets:
        console.print("[dim]No budgets configured.[/dim]")
        return

    table = Table(
        title="Budgets",
        title_style="bold",
        show_lines=False,
        padding=(0, 2),
    )
    table.add_column("Scope", style="dim")
    table.add_column("ID", style="bold")
    table.add_column("Spent", justify="right")
    table.add_column("Limit", justify="right")
    table.add_column("Used", justify="right")
    table.add_column("Status")

    for b in budgets:
        scope = b.get("scope", "")
        scope_id = b.get("scope_id", "")
        spent = b.get("spent", 0.0)
        limit = b.get("limit", 0.0)
        percentage = b.get("percentage", 0.0)

        color = _pct_color(percentage)
        status_label = _pct_status(percentage)

        table.add_row(
            scope,
            scope_id,
            f"${spent:.2f}",
            f"${limit:.2f}",
            f"[{color}]{percentage:.0f}%[/{color}]",
            f"[{color}]{status_label}[/{color}]",
        )

    console.print(table)


@budget.command("set")
@click.argument("scope", type=click.Choice(["agent", "user", "team", "global"]))
@click.argument("id_")
@click.argument("amount", type=float)
@click.pass_context
def budget_set(ctx: click.Context, scope: str, id_: str, amount: float) -> None:
    """Set a budget limit for a scope/id pair.

    Example: budgetctl budget set agent email-agent 50.00
    """
    data = _request(
        ctx,
        "PUT",
        f"/api/budgets/{scope}/{id_}",
        json_body={"limit": amount},
    )

    if ctx.obj["format"] == "json":
        _output_json(data)
        return

    console.print(
        f"[green]Budget set:[/green] {scope}/{id_} = [bold]${amount:.2f}[/bold]"
    )


@budget.command("get")
@click.argument("scope", type=click.Choice(["agent", "user", "team", "global"]))
@click.argument("id_")
@click.pass_context
def budget_get(ctx: click.Context, scope: str, id_: str) -> None:
    """Get budget details for a single scope/id pair.

    Example: budgetctl budget get agent email-agent
    """
    data = _request(ctx, "GET", f"/api/budgets/{scope}/{id_}")

    if ctx.obj["format"] == "json":
        _output_json(data)
        return

    if not data:
        console.print(f"[dim]No budget found for {scope}/{id_}[/dim]")
        return

    spent = data.get("spent", 0.0)
    limit = data.get("limit", 0.0)
    remaining = data.get("remaining", limit - spent)
    percentage = data.get("percentage", 0.0)
    color = _pct_color(percentage)
    status_label = _pct_status(percentage)

    lines = [
        f"[bold]Scope:[/bold]     {data.get('scope', scope)}",
        f"[bold]ID:[/bold]        {data.get('scope_id', id_)}",
        f"[bold]Spent:[/bold]     ${spent:.2f}",
        f"[bold]Limit:[/bold]     ${limit:.2f}",
        f"[bold]Remaining:[/bold] ${remaining:.2f}",
        f"[bold]Used:[/bold]      [{color}]{percentage:.0f}%[/{color}]",
        f"[bold]Status:[/bold]    [{color}]{status_label}[/{color}]",
    ]

    panel = Panel(
        "\n".join(lines),
        title=f"[bold cyan]Budget: {scope}/{id_}[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------


if __name__ == "__main__":
    cli()
