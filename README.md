# Flare Lookup CLI

A command-line tool to run **global search** lookups against the [Flare API](https://api.docs.flare.io/) and export events and credentials to JSON, JSONL, or CSV. Uses Flare’s token-based auth and follows pagination so large result sets are fully exported.

**Not the official Flare CLI.** The official [flareio-cli](https://api.docs.flare.io/sdk/cli) focuses on exporting data for your tenant and identifiers. This tool calls the **global search** endpoints so you can run ad-hoc lookups (e.g. by domain, email, keyword) without configuring identifiers first. Global search counts against your [search quota](https://api.docs.flare.io/concepts/rate-limits-and-quotas).

## Features

- **Search credentials** by domain, email, keyword, secret, or auth domain
- **Search events** by keyword, domain, email, query string, or username
- **Export** to JSON, JSONL, CSV, or all three at once (with pagination so all pages are fetched)
- **Rate limiting** and 429 retry with backoff to stay within Flare limits
- **Verbose logging** (`-v` / `-vv`) of API requests to stderr for debugging
- **Optional tenant** scope via `--tenant` for token generation

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (or pip)
- A [Flare](https://flare.io) account and API key

## Installation

```bash
git clone https://github.com/l0lsec/flare-lookup-cli.git
cd flare-lookup-cli
uv sync
```

`uv sync` installs into the project's `.venv`, so run the CLI through `uv run` from the project directory:

```bash
uv run flare-lookup --help
```

All examples below use `uv run flare-lookup`. To call `flare-lookup` directly from any directory, install it as a uv tool and drop the `uv run` prefix:

```bash
uv tool install --editable .
```

(Or, inside an activated virtualenv: `pip install -e .`)

## Configuration

| Variable         | Description                         |
|------------------|-------------------------------------|
| `FLARE_API_KEY`  | Your Flare API key (required)       |

```bash
export FLARE_API_KEY="your-api-key"
```

Create an API key under [Flare Profile → API Keys](https://app.flare.io/#/profile). You can also pass `--api-key` on the command line instead of using the env var.

## Usage

### Search credentials

Query the global credentials search and export results. Default page size is 10,000; pagination is automatic until there are no more results.

```bash
# By domain (default query type), export to CSV (saved as results/creds.csv)
uv run flare-lookup search-credentials --domain example.com --output creds.csv --format csv

# Export JSON, JSONL, and CSV in one run (results/creds.json, .jsonl, .csv)
uv run flare-lookup search-credentials -d example.com -o creds --format all

# By email
uv run flare-lookup search-credentials -q email -e user@example.com -o creds.json

# By keyword (username part of identity)
uv run flare-lookup search-credentials -q keyword -k "admin" -o creds.jsonl --format jsonl

# Filter by imported date (ISO-8601)
uv run flare-lookup search-credentials -d example.com -o out.csv --format csv \
  --imported-after 2024-01-01T00:00:00Z --imported-before 2024-12-31T23:59:59Z
```

**Query types:** `domain`, `email`, `keyword`, `secret`, `auth_domain`

### Search events

Query the global events search (paste, stealer_log, listing, etc.). The API returns at most 10 events per page, so large searches make many requests; use `--max-pages` to cap them.

```bash
# By keyword
uv run flare-lookup search-events -q keyword -k "fraud" -o events.json

# By domain, first 5 pages only
uv run flare-lookup search-events -q domain -d example.com -o events.csv --format csv --max-pages 5

# Filter by event type, severity, and time window
uv run flare-lookup search-events -q keyword -k "leak" --types paste,stealer_log \
  --severity high,critical --created-after 2024-01-01T00:00:00Z -o out.json
```

**Query types:** `keyword`, `domain`, `email`, `query_string`, `username`

### Options

Shared by `search-credentials` and `search-events`:

| Option | Description |
|--------|-------------|
| `-o, --output PATH` | File to write. A bare file name (e.g. `creds.csv`) is saved under `--output-dir`; a path with a directory (e.g. `/tmp/creds.csv`) is used as given. Without `--output`, the first 20 results are printed to the terminal as JSON. |
| `--output-dir DIR` | Where bare `--output` file names are saved (default: `results/`, relative to the current directory). |
| `-f, --format` | `json` (default), `jsonl`, `csv`, or `all`. The format is **not** inferred from the file extension, so pass `--format csv` when writing a `.csv` file. |
| `-n, --max-pages N` | Stop after N pages (default: fetch all). |
| `-s, --size N` | Results per page. Credentials: default and max 10,000. Events: default and max 10. |
| `--order` | `desc` (default) or `asc`. |
| `--api-key KEY` | API key to use instead of `FLARE_API_KEY`. |
| `--tenant ID` | Tenant ID to scope the generated token. |
| `-v, --verbose` | `-v` logs each API request and response status to stderr; `-vv` also logs request payloads and a truncated token. |

Filters for `search-credentials`: `--imported-after`, `--imported-before` (ISO-8601).

Filters for `search-events`: `--types` (comma-separated event types), `--severity` (a minimum severity, or a comma list of `info`, `low`, `medium`, `high`, `critical`), `--created-after`, `--created-before` (ISO-8601).

Run `uv run flare-lookup <command> --help` for the full list.

### Token (debug)

Print a short-lived Bearer token:

```bash
uv run flare-lookup token
# Optional: uv run flare-lookup token --tenant <tenant-id>
```

## Output formats

- **json** – Single JSON array (default)
- **jsonl** – Newline-delimited JSON, one object per line
- **csv** – Flattened table (credentials: `imported_at`, `indicator_of_identity`, `domain`, `hash`, `hash_type`, `source`, `source_id`, `id`; events: `uid`, `type`, `estimated_created_at`, `matched_at`, `severity`)
- **all** – Writes every format above from a single search. The output path's extension is swapped per format (e.g. `-o results.json --format all` → `results.json`, `results.jsonl`, `results.csv`); a path with no known extension gets one appended.

Exports are saved to `results/` by default (e.g. `-o creds.csv` → `results/creds.csv`), which is gitignored. `.gitignore` also excludes `*.csv`, `*.json`, and `*_creds.jsonl` anywhere in the repo.

## Rate limits and pagination

- Credentials and events search use Flare’s [paging](https://api.docs.flare.io/concepts/paging) (`from` / `next`). The CLI follows all pages until `next` is null (or `--max-pages` is reached).
- A 1-second delay is applied between page requests to reduce 429s. On 429, the CLI retries up to 3 times with exponential backoff (2s, 4s, 8s) before failing.
- When running many domains in a loop, consider adding a short delay between invocations (e.g. `sleep 2`) to stay under rate limits.

## API reference

- [Flare API – Getting started](https://api.docs.flare.io/introduction/getting-started)
- [Authentication](https://api.docs.flare.io/concepts/authentication)
- [Tokens – Generate](https://api.docs.flare.io/api-reference/tokens/endpoints/generate)
- [Credentials global search](https://api.docs.flare.io/api-reference/v4/endpoints/credentials-global-search)
- [Events global search](https://api.docs.flare.io/api-reference/v4/endpoints/global-search)
- [Paging](https://api.docs.flare.io/concepts/paging)

## License

MIT.
