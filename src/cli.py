import json
import os

import httpx
import typer
import uvicorn

app = typer.Typer(help="CLI client and server runner for the Photonics Bench Service.")
DEFAULT_URL = os.environ.get("BENCH_SERVICE_URL", "http://127.0.0.1:8000")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    """Start the instrument service."""
    uvicorn.run("service.app:app", host=host, port=port, reload=False)


@app.command()
def status(url: str = typer.Option(DEFAULT_URL, "--url", help="Service base URL")) -> None:
    """Show connection and health status of all instruments on the bench."""
    try:
        resp = httpx.get(f"{url}/health", timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        typer.secho(f"Error reaching service at {url}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    data = resp.json()
    active = data.get("active_run_id") or "None (Idle)"
    typer.echo(f"Bench Active Sweep: {active}\n")
    typer.echo(f"{'INSTRUMENT':<12} {'STATUS':<15} {'RESOURCE':<14} {'SERIAL':<14} {'LAST ERROR'}")
    typer.echo("-" * 78)
    for inst in data["instruments"]:
        st = inst["status"]
        color = (
            typer.colors.GREEN
            if st == "connected"
            else typer.colors.YELLOW
            if st == "busy"
            else typer.colors.RED
        )
        line = (
            f"{inst['name']:<12} "
            f"{typer.style(f'{st:<15}', fg=color)} "
            f"{inst['resource']:<14} "
            f"{inst['serial_number']:<14} "
            f"{inst.get('last_error') or '-'}"
        )
        typer.echo(line)


@app.command()
def sweep(
    start: float = typer.Option(..., "--start", help="Start wavelength (nm)"),
    stop: float = typer.Option(..., "--stop", help="Stop wavelength (nm)"),
    step: float = typer.Option(0.1, "--step", help="Step size (nm)"),
    operator: str = typer.Option(
        os.environ.get("USER", "researcher"), "--operator", help="Operator name"
    ),
    notes: str = typer.Option("", "--notes", help="Run notes/context"),
    url: str = typer.Option(DEFAULT_URL, "--url", help="Service base URL"),
) -> None:
    """Run a wavelength sweep and stream live readings as they arrive."""
    payload = {
        "start_nm": start,
        "stop_nm": stop,
        "step_nm": step,
        "operator": operator,
        "notes": notes,
    }
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", f"{url}/sweeps", json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    typer.secho(
                        f"Sweep rejected ({resp.status_code}): {body}",
                        fg=typer.colors.RED,
                        err=True,
                    )
                    raise typer.Exit(code=1)

                for line in resp.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    etype = event.get("type")
                    if etype == "started":
                        typer.secho(
                            f"Started Run ID: {event['run_id']} ({event['total_points']} points)",
                            fg=typer.colors.CYAN,
                        )
                        typer.echo(
                            f"{'IDX':<5} {'TARGET (nm)':<13} {'ACTUAL (nm)':<13} {'POWER (dBm)':<13} {'TRIES':<6} {'NOTE'}"
                        )
                    elif etype == "point":
                        power = (
                            f"{event['power_dbm']:.4f}"
                            if event["power_dbm"] is not None
                            else "FAILED"
                        )
                        actual = (
                            f"{event['actual_nm']:.4f}" if event["actual_nm"] is not None else "-"
                        )
                        note = event.get("error_msg") or ""
                        color = typer.colors.RED if event["power_dbm"] is None else None
                        row = f"{event['step_index']:<5} {event['target_nm']:<13.4f} {actual:<13} {power:<13} {event['attempts']:<6} {note}"
                        typer.secho(row, fg=color)
                    elif etype == "complete":
                        typer.secho(
                            f"\nRun {event['run_id']} finished: status={event['status']}, "
                            f"failed_points={event['failed_points']}/{event['total_points']}",
                            fg=typer.colors.GREEN
                            if event["status"] == "completed"
                            else typer.colors.YELLOW,
                        )
    except httpx.HTTPError as exc:
        typer.secho(f"Connection error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("list")
def list_runs(
    limit: int = typer.Option(20, "--limit", help="Max runs to show"),
    url: str = typer.Option(DEFAULT_URL, "--url", help="Service base URL"),
) -> None:
    """List past sweep runs stored by the service."""
    resp = httpx.get(f"{url}/sweeps", params={"limit": limit}, timeout=5.0)
    resp.raise_for_status()
    runs = resp.json()
    if not runs:
        typer.echo("No runs recorded yet.")
        return

    typer.echo(
        f"{'RUN ID':<38} {'CREATED (UTC)':<22} {'RANGE (nm)':<18} {'POINTS':<9} {'STATUS':<11} {'OPERATOR'}"
    )
    typer.echo("-" * 112)
    for r in runs:
        rng = f"{r['start_nm']:.1f}-{r['stop_nm']:.1f}"
        pts = f"{r['valid_points']}/{r['point_count']}"
        typer.echo(
            f"{r['run_id']:<38} {r['created_at'][:19]:<22} {rng:<18} {pts:<9} {r['status']:<11} {r['operator']}"
        )


@app.command()
def show(
    run_id: str = typer.Argument(..., help="UUID of the run to inspect"),
    url: str = typer.Option(DEFAULT_URL, "--url", help="Service base URL"),
) -> None:
    """Reopen a past run with full provenance metadata and readings."""
    resp = httpx.get(f"{url}/sweeps/{run_id}", timeout=5.0)
    if resp.status_code == 404:
        typer.secho(f"Run {run_id} not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    resp.raise_for_status()
    data = resp.json()

    typer.secho(f"=== Run {data['run_id']} ===", fg=typer.colors.CYAN)
    typer.echo(f"Created:    {data['created_at']}")
    typer.echo(f"Completed:  {data['completed_at']}")
    typer.echo(f"Operator:   {data['operator']}")
    typer.echo(f"Notes:      {data['notes'] or '-'}")
    typer.echo(f"Status:     {data['status']}")
    typer.echo(
        f"Sweep:      {data['start_nm']} -> {data['stop_nm']} nm (step {data['step_nm']} nm)"
    )
    typer.echo(f"Provenance: {json.dumps(data['provenance'])}")
    typer.echo("\nReadings:")
    typer.echo(
        f"{'IDX':<5} {'TARGET (nm)':<13} {'ACTUAL (nm)':<13} {'POWER (dBm)':<13} {'TRIES':<6} {'ERROR'}"
    )
    for pt in data["readings"]:
        power = f"{pt['power_dbm']:.4f}" if pt["power_dbm"] is not None else "FAILED"
        actual = f"{pt['actual_nm']:.4f}" if pt["actual_nm"] is not None else "-"
        typer.echo(
            f"{pt['step_index']:<5} {pt['target_nm']:<13.4f} {actual:<13} {power:<13} {pt['attempts']:<6} {pt['error_msg'] or ''}"
        )


if __name__ == "__main__":
    app()
