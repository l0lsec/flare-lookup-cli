#!/usr/bin/env python3
"""
Flare API Lookup CLI — search events and credentials via Flare API and export to JSON/CSV.

Authentication: set FLARE_API_KEY or pass --api-key. Tokens are generated via
POST https://api.flare.io/tokens/generate and used as Bearer for all requests.

Docs: https://api.docs.flare.io/
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import typer
import requests
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

app = typer.Typer(
    name="flare-lookup",
    help="Lookup Flare API (events & credentials) and export to JSON/CSV.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True, highlight=False)

BASE_URL = "https://api.flare.io"
DEFAULT_EVENT_PAGE_SIZE = 10   # API max for events
DEFAULT_CRED_PAGE_SIZE = 10_000
MAX_CRED_PAGE_SIZE = 10_000
DEFAULT_OUTPUT_DIR = Path("results")  # gitignored

VERBOSITY = 0  # set per-command from -v/--verbose count flag


def set_verbosity(level: int) -> None:
    """Set the module-level verbosity used by ``vlog``."""
    global VERBOSITY
    VERBOSITY = level


def vlog(level: int, msg: str) -> None:
    """Log ``msg`` to stderr if current verbosity is at least ``level``."""
    if VERBOSITY >= level:
        err_console.log(msg)


def get_token(api_key: str, tenant: str | None = None) -> str:
    """Obtain a short-lived API token using the API key."""
    url = f"{BASE_URL}/tokens/generate"
    headers = {"Authorization": api_key}
    params = {} if not tenant else {"tenant": tenant}
    vlog(1, f"POST /tokens/generate tenant={tenant!r}")
    r = requests.post(url, headers=headers, params=params, timeout=30)
    vlog(1, f"  ← {r.status_code} {r.reason} ({len(r.content)} bytes)")
    r.raise_for_status()
    data = r.json()
    token = data.get("token")
    if not token:
        raise SystemExit("Token response missing 'token' field.")
    vlog(2, f"token acquired: {token[:8]}… (len={len(token)})")
    return token


def make_session(api_key: str, tenant: str | None = None) -> requests.Session:
    """Create a requests session with Bearer token from api_key."""
    token = get_token(api_key, tenant)
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    s.headers["Content-Type"] = "application/json"
    return s


# ---------- Events global search ----------
# POST /firework/v4/events/global/_search
# Body: query (domain|email|keyword|query_string|username|...), size (max 10), from, order, filters

def build_events_query(
    query_type: str,
    keyword: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    query_string: str | None = None,
    username: str | None = None,
) -> dict[str, Any]:
    """Build the 'query' object for events global search."""
    if query_type == "keyword" and keyword:
        return {"type": "keyword", "keyword": keyword}
    if query_type == "domain" and domain:
        return {"type": "domain", "fqdn": domain}
    if query_type == "email" and email:
        return {"type": "email", "email": email}
    if query_type == "query_string" and query_string:
        return {"type": "query_string", "query_string": query_string}
    if query_type == "username" and username:
        return {"type": "username", "username": username}
    raise typer.BadParameter(
        f"Missing value for query type '{query_type}'. "
        "Use --keyword, --domain, --email, --query-string, or --username as appropriate."
    )


def search_events_page(
    session: requests.Session,
    query: dict[str, Any],
    size: int = DEFAULT_EVENT_PAGE_SIZE,
    from_: str | None = None,
    order: str = "desc",
    event_types: list[str] | None = None,
    severity: str | list[str] | None = None,
    estimated_created_at_gte: str | None = None,
    estimated_created_at_lte: str | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one page of events global search. Returns (items, next)."""
    url = f"{BASE_URL}/firework/v4/events/global/_search"
    payload: dict[str, Any] = {
        "query": query,
        "size": min(size, DEFAULT_EVENT_PAGE_SIZE),
        "order": order,
    }
    if from_:
        payload["from"] = from_
    filters: dict[str, Any] = {}
    if event_types:
        filters["type"] = event_types
    if severity is not None:
        filters["severity"] = severity
    if estimated_created_at_gte is not None:
        filters["estimated_created_at"] = filters.get("estimated_created_at") or {}
        filters["estimated_created_at"]["gte"] = estimated_created_at_gte
    if estimated_created_at_lte is not None:
        filters["estimated_created_at"] = filters.get("estimated_created_at") or {}
        filters["estimated_created_at"]["lte"] = estimated_created_at_lte
    if filters:
        payload["filters"] = filters

    vlog(1, f"POST /firework/v4/events/global/_search from={from_!r} size={payload['size']}")
    vlog(2, f"  payload={json.dumps(payload, default=str)[:500]}")
    for attempt in range(4):
        if attempt > 0:
            backoff = 2 ** attempt
            vlog(1, f"  retry attempt {attempt + 1}/4 after {backoff}s backoff")
            time.sleep(backoff)
        r = session.post(url, json=payload, timeout=60)
        if r.status_code == 429:
            vlog(1, f"  ← 429 Too Many Requests (attempt {attempt + 1}/4)")
            continue
        vlog(1, f"  ← {r.status_code} {r.reason} ({len(r.content)} bytes)")
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        next_cursor = data.get("next")
        vlog(1, f"  ← {len(items)} items, next={'yes' if next_cursor else 'no'}")
        return items, next_cursor
    r.raise_for_status()
    return [], None


def iter_events(
    session: requests.Session,
    query: dict[str, Any],
    size: int = DEFAULT_EVENT_PAGE_SIZE,
    order: str = "desc",
    event_types: list[str] | None = None,
    severity: str | list[str] | None = None,
    estimated_created_at_gte: str | None = None,
    estimated_created_at_lte: str | None = None,
    max_pages: int | None = None,
) -> Iterator[tuple[int, list[dict], str | None]]:
    """Yield ``(page_index, items, next_cursor)`` tuples from events search.

    Pagination follows ``next`` until exhausted or ``max_pages`` is reached.
    """
    from_ = None
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break
        page += 1
        items, next_cursor = search_events_page(
            session,
            query,
            size=size,
            from_=from_,
            order=order,
            event_types=event_types,
            severity=severity,
            estimated_created_at_gte=estimated_created_at_gte,
            estimated_created_at_lte=estimated_created_at_lte,
        )
        yield page, items, next_cursor
        if not next_cursor:
            break
        from_ = next_cursor
        vlog(2, "sleeping 1s between pages (rate limit)")
        time.sleep(1)  # rate limit: avoid 429 from Flare API


# ---------- Credentials global search ----------
# POST /firework/v4/credentials/global/_search
# Body: query (domain|email|keyword|secret|auth_domain), size (max 10000), from, order, filters

def build_credentials_query(
    query_type: str,
    domain: str | None = None,
    email: str | None = None,
    keyword: str | None = None,
    secret: str | None = None,
    auth_domain: str | None = None,
) -> dict[str, Any]:
    """Build the 'query' object for credentials global search."""
    if query_type == "domain" and domain:
        return {"type": "domain", "fqdn": domain}
    if query_type == "email" and email:
        return {"type": "email", "email": email}
    if query_type == "keyword" and keyword:
        return {"type": "keyword", "keyword": keyword}
    if query_type == "secret" and secret:
        return {"type": "secret", "secret": secret}
    if query_type == "auth_domain" and auth_domain:
        return {"type": "auth_domain", "fqdn": auth_domain}
    raise typer.BadParameter(
        f"Missing value for query type '{query_type}'. "
        "Use --domain, --email, --keyword, --secret, or --auth-domain as appropriate."
    )


def search_credentials_page(
    session: requests.Session,
    query: dict[str, Any],
    size: int = DEFAULT_CRED_PAGE_SIZE,
    from_: str | None = None,
    order: str = "desc",
    imported_at_gte: str | None = None,
    imported_at_lte: str | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one page of credentials global search. Returns (items, next)."""
    url = f"{BASE_URL}/firework/v4/credentials/global/_search"
    payload: dict[str, Any] = {
        "query": query,
        "size": min(size, MAX_CRED_PAGE_SIZE),
        "order": order,
    }
    if from_:
        payload["from"] = from_
    if imported_at_gte is not None or imported_at_lte is not None:
        payload["filters"] = {
            "imported_at": {
                **({"gte": imported_at_gte} if imported_at_gte else {}),
                **({"lte": imported_at_lte} if imported_at_lte else {}),
            }
        }

    vlog(1, f"POST /firework/v4/credentials/global/_search from={from_!r} size={payload['size']}")
    vlog(2, f"  payload={json.dumps(payload, default=str)[:500]}")
    for attempt in range(4):
        if attempt > 0:
            backoff = 2 ** attempt  # 2, 4, 8 sec backoff
            vlog(1, f"  retry attempt {attempt + 1}/4 after {backoff}s backoff")
            time.sleep(backoff)
        r = session.post(url, json=payload, timeout=60)
        if r.status_code == 429:
            vlog(1, f"  ← 429 Too Many Requests (attempt {attempt + 1}/4)")
            continue
        vlog(1, f"  ← {r.status_code} {r.reason} ({len(r.content)} bytes)")
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or []
        next_cursor = data.get("next")
        vlog(1, f"  ← {len(items)} items, next={'yes' if next_cursor else 'no'}")
        return items, next_cursor
    r.raise_for_status()  # raise 429 after retries
    return [], None


def iter_credentials(
    session: requests.Session,
    query: dict[str, Any],
    size: int = DEFAULT_CRED_PAGE_SIZE,
    order: str = "desc",
    imported_at_gte: str | None = None,
    imported_at_lte: str | None = None,
    max_pages: int | None = None,
) -> Iterator[tuple[int, list[dict], str | None]]:
    """Yield ``(page_index, items, next_cursor)`` tuples from credentials search."""
    from_ = None
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break
        page += 1
        items, next_cursor = search_credentials_page(
            session,
            query,
            size=size,
            from_=from_,
            order=order,
            imported_at_gte=imported_at_gte,
            imported_at_lte=imported_at_lte,
        )
        yield page, items, next_cursor
        if not next_cursor:
            break
        from_ = next_cursor
        vlog(2, "sleeping 1s between pages (rate limit)")
        time.sleep(1)  # rate limit: avoid 429 from Flare API


# ---------- Export ----------

def write_json(path: Path, items: list[dict]) -> None:
    """Write items as JSON array to path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, default=str)


def write_jsonl(path: Path, items: list[dict]) -> None:
    """Write items as newline-delimited JSON."""
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, default=str) + "\n")


def credential_to_csv_row(c: dict) -> dict[str, str]:
    """Flatten one credential for CSV (align with existing Flare export style)."""
    source = c.get("source") or {}
    return {
        "imported_at": (c.get("imported_at") or ""),
        "indicator_of_identity": (c.get("identity_name") or ""),
        "domain": (c.get("domain") or ""),
        "hash": (c.get("hash") or ""),
        "hash_type": (c.get("hash_type") or ""),
        "source": (source.get("name") or c.get("source_id") or ""),
        "source_id": (c.get("source_id") or ""),
        "id": str(c.get("id") or ""),
    }


def write_credentials_csv(path: Path, items: list[dict]) -> None:
    """Write credentials to CSV with standard columns."""
    if not items:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("imported_at,indicator_of_identity,domain,hash,hash_type,source,source_id,id\n")
        return
    rows = [credential_to_csv_row(c) for c in items]
    keys = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def write_events_csv(path: Path, items: list[dict]) -> None:
    """Write events to CSV with flattened metadata/highlights."""
    if not items:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("uid,type,estimated_created_at,matched_at,severity\n")
        return
    rows = []
    for ev in items:
        meta = ev.get("metadata") or {}
        rows.append({
            "uid": meta.get("uid", ""),
            "type": meta.get("type", ""),
            "estimated_created_at": meta.get("estimated_created_at", ""),
            "matched_at": meta.get("matched_at", ""),
            "severity": meta.get("severity", ""),
        })
    keys = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


OUTPUT_FORMATS = ("json", "jsonl", "csv")


def resolve_output(output: Path, output_dir: Path) -> Path:
    """Place a bare filename under ``output_dir``; paths with a directory are used as given."""
    if output.is_absolute() or output.parent != Path("."):
        return output
    return output_dir / output


def export_results(
    output: Path,
    fmt: str,
    items: list[dict],
    csv_writer: Callable[[Path, list[dict]], None],
) -> list[Path]:
    """Write items to ``output`` in ``fmt``; ``all`` writes one file per format.

    For ``all``, a known format suffix on ``output`` is replaced (``out.json`` →
    ``out.json``, ``out.jsonl``, ``out.csv``); otherwise the suffix is appended.
    Returns the paths written.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "all":
        base = output.with_suffix("") if output.suffix.lstrip(".").lower() in OUTPUT_FORMATS else output
        targets = [(f, base.with_name(f"{base.name}.{f}")) for f in OUTPUT_FORMATS]
    else:
        targets = [(fmt, output)]
    for f, path in targets:
        if f == "csv":
            csv_writer(path, items)
        elif f == "jsonl":
            write_jsonl(path, items)
        else:
            write_json(path, items)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = -1
        vlog(1, f"wrote {path} format={f} bytes={size_bytes}")
    return [path for _, path in targets]


# ---------- Commands ----------

@app.command()
def search_events(
    query_type: str = typer.Option(
        "keyword",
        "--query-type",
        "-q",
        help="Query type: keyword, domain, email, query_string, username",
    ),
    keyword: str | None = typer.Option(None, "--keyword", "-k", help="Keyword (for query_type=keyword)"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain / FQDN (for query_type=domain)"),
    email: str | None = typer.Option(None, "--email", "-e", help="Email (for query_type=email)"),
    query_string: str | None = typer.Option(None, "--query-string", help="Lucene query (for query_type=query_string)"),
    username: str | None = typer.Option(None, "--username", "-u", help="Username (for query_type=username)"),
    output: Path | None = typer.Option(None, "--output", "-o", path_type=Path, help="Output file name (bare names go in --output-dir)"),
    output_dir: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--output-dir", path_type=Path, help="Directory for bare --output file names"),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, jsonl, csv, or all (writes one file per format)"),
    size: int = typer.Option(DEFAULT_EVENT_PAGE_SIZE, "--size", "-s", help="Page size (max 10 for events)"),
    max_pages: int | None = typer.Option(None, "--max-pages", "-n", help="Stop after N pages (default: all)"),
    order: str = typer.Option("desc", "--order", help="Order: asc or desc"),
    event_types: str | None = typer.Option(None, "--types", help="Comma-separated event types (e.g. paste,stealer_log)"),
    severity: str | None = typer.Option(None, "--severity", help="Min severity or comma list: info,low,medium,high,critical"),
    created_after: str | None = typer.Option(None, "--created-after", help="ISO-8601 timestamp (estimated_created_at gte)"),
    created_before: str | None = typer.Option(None, "--created-before", help="ISO-8601 timestamp (estimated_created_at lte)"),
    api_key: str | None = typer.Option(None, "--api-key", envvar="FLARE_API_KEY", help="Flare API key (or set FLARE_API_KEY)"),
    tenant: str | None = typer.Option(None, "--tenant", help="Tenant ID for token (optional)"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity (-v request log, -vv payloads/token)"),
) -> None:
    """Search Flare events globally and optionally export to file."""
    set_verbosity(verbose)
    if not api_key:
        console.print("[red]FLARE_API_KEY not set and --api-key not provided.[/red]")
        raise typer.Exit(1)
    query = build_events_query(
        query_type=query_type,
        keyword=keyword,
        domain=domain,
        email=email,
        query_string=query_string,
        username=username,
    )
    types_list = [t.strip() for t in event_types.split(",")] if event_types else None
    severity_val: str | list[str] | None = None
    if severity:
        parts = [p.strip() for p in severity.split(",")]
        severity_val = parts[0] if len(parts) == 1 else parts

    vlog(1, f"events search query={query} filters types={types_list} severity={severity_val}")
    session = make_session(api_key, tenant)
    collected: list[dict] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("• page {task.fields[page]} • {task.fields[items]} items"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Searching events…", total=None, page=0, items=0)
        for page_idx, items, next_cursor in iter_events(
            session,
            query,
            size=size,
            order=order,
            event_types=types_list,
            severity=severity_val,
            estimated_created_at_gte=created_after,
            estimated_created_at_lte=created_before,
            max_pages=max_pages,
        ):
            collected.extend(items)
            progress.update(task_id=task, page=page_idx, items=len(collected))
        progress.update(
            task_id=task,
            description=f"Found {len(collected)} events",
        )
    console.print(f"[green]Total events: {len(collected)}[/green]")
    if output:
        output = resolve_output(output, output_dir)
        fmt = format.lower() or (output.suffix.lstrip(".") if output.suffix else "json")
        for path in export_results(output, fmt, collected, write_events_csv):
            console.print(f"[green]Wrote [bold]{path}[/bold][/green]")
    else:
        # Print first 20 as JSON to stdout
        for item in collected[:20]:
            console.print_json(data=item)
        if len(collected) > 20:
            console.print(f"... and {len(collected) - 20} more (use --output to export all)")


@app.command()
def search_credentials(
    query_type: str = typer.Option(
        "domain",
        "--query-type",
        "-q",
        help="Query type: domain, email, keyword, secret, auth_domain",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain / FQDN (for domain or auth_domain)"),
    email: str | None = typer.Option(None, "--email", "-e", help="Email (for query_type=email)"),
    keyword: str | None = typer.Option(None, "--keyword", "-k", help="Keyword (username part of identity)"),
    secret: str | None = typer.Option(None, "--secret", help="Password/secret (for query_type=secret)"),
    auth_domain: str | None = typer.Option(None, "--auth-domain", help="Auth domain (for query_type=auth_domain)"),
    output: Path | None = typer.Option(None, "--output", "-o", path_type=Path, help="Output file name (bare names go in --output-dir)"),
    output_dir: Path = typer.Option(DEFAULT_OUTPUT_DIR, "--output-dir", path_type=Path, help="Directory for bare --output file names"),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, jsonl, csv, or all (writes one file per format)"),
    size: int = typer.Option(DEFAULT_CRED_PAGE_SIZE, "--size", "-s", help="Page size (max 10000)"),
    max_pages: int | None = typer.Option(None, "--max-pages", "-n", help="Stop after N pages (default: all)"),
    order: str = typer.Option("desc", "--order", help="Order: asc or desc"),
    imported_after: str | None = typer.Option(None, "--imported-after", help="ISO-8601 (imported_at gte)"),
    imported_before: str | None = typer.Option(None, "--imported-before", help="ISO-8601 (imported_at lte)"),
    api_key: str | None = typer.Option(None, "--api-key", envvar="FLARE_API_KEY", help="Flare API key (or set FLARE_API_KEY)"),
    tenant: str | None = typer.Option(None, "--tenant", help="Tenant ID for token (optional)"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity (-v request log, -vv payloads/token)"),
) -> None:
    """Search Flare credentials globally and optionally export to file."""
    set_verbosity(verbose)
    if not api_key:
        console.print("[red]FLARE_API_KEY not set and --api-key not provided.[/red]")
        raise typer.Exit(1)
    query = build_credentials_query(
        query_type=query_type,
        domain=domain,
        email=email,
        keyword=keyword,
        secret=secret,
        auth_domain=auth_domain,
    )
    vlog(1, f"credentials search query={query} imported_at gte={imported_after} lte={imported_before}")
    session = make_session(api_key, tenant)
    collected: list[dict] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("• page {task.fields[page]} • {task.fields[items]} items"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Searching credentials…", total=None, page=0, items=0)
        for page_idx, items, next_cursor in iter_credentials(
            session,
            query,
            size=size,
            order=order,
            imported_at_gte=imported_after,
            imported_at_lte=imported_before,
            max_pages=max_pages,
        ):
            collected.extend(items)
            progress.update(task_id=task, page=page_idx, items=len(collected))
        progress.update(
            task_id=task,
            description=f"Found {len(collected)} credentials",
        )
    console.print(f"[green]Total credentials: {len(collected)}[/green]")
    if output:
        output = resolve_output(output, output_dir)
        fmt = format.lower() or (output.suffix.lstrip(".") if output.suffix else "json")
        for path in export_results(output, fmt, collected, write_credentials_csv):
            console.print(f"[green]Wrote [bold]{path}[/bold][/green]")
    else:
        for item in collected[:20]:
            console.print_json(data=item)
        if len(collected) > 20:
            console.print(f"... and {len(collected) - 20} more (use --output to export all)")


@app.command()
def token(
    api_key: str | None = typer.Option(None, "--api-key", envvar="FLARE_API_KEY"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    """Generate and print a Flare API token (for debugging)."""
    if not api_key:
        console.print("[red]FLARE_API_KEY not set and --api-key not provided.[/red]")
        raise typer.Exit(1)
    t = get_token(api_key, tenant)
    console.print(t)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
