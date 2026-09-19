# Xingzhe MCP

English · [简体中文](README.zh-CN.md)

Bring your [Xingzhe](https://www.imxingzhe.com/) rides into conversations with AI assistants. Query activities, review a ride, and compare cadence, heart rate, or power when those measurements are available.

Python · FastAPI · MCP · Supabase · Vercel. Managed with [uv](https://docs.astral.sh/uv/).

## Why this project?

Your rides already live in the mainland China Xingzhe app. This service connects to its official API and exposes two read-only MCP tools. One deployment supports multiple Xingzhe accounts. Each user authorizes their own account when connecting an MCP client.

| Tool | What it does |
| --- | --- |
| `list_activities` | Lists activities with pagination and optional start/end timestamps. |
| `get_activity` | Returns one activity's details, including sensor summaries supplied by Xingzhe. |

Timestamps used as filters are Unix milliseconds. Distance is in meters and duration in seconds. Other fields retain the upstream values; missing measurements are not inferred. The activities API does not provide per-second cadence streams or FIT downloads.

## Getting started

You need a [Xingzhe developer application](https://www.imxingzhe.com/home/#/settings/api), a Supabase project, and an MCP client that supports OAuth with a configured client ID and secret.

```sh
uv sync --locked
cp .env.example .env
```

1. Run the SQL in [`supabase/migrations/`](supabase/migrations/) once in your Supabase SQL editor, or apply it through your existing migration workflow. It creates a private `xingzhe` schema.
2. Fill in `.env`. Use the **transaction pooler** connection string from Supabase's Connect dialog for `DATABASE_URL`, with `sslmode=require`. Keep the actual pooler host from the dashboard. `POSTGRES_URL` is also accepted, so the Vercel integration can supply it directly.
3. Generate `ENCRYPTION_KEY` and `MCP_CLIENT_SECRET` using the commands in [`.env.example`](.env.example). Generate each secret separately and keep the encryption key stable across deployments.
4. Set your Xingzhe application's callback to `PUBLIC_URL/oauth/xingzhe/callback`. For local development, that is `http://localhost:8000/oauth/xingzhe/callback`.
5. Set `MCP_REDIRECT_URIS` to a JSON array of exact callback URLs supplied by your MCP client. These are different from the Xingzhe callback. No wildcards are accepted.

```sh
uv run uvicorn app:app --reload --no-access-log
```

Add `PUBLIC_URL/mcp` to your MCP client using `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET`. Connecting opens Xingzhe’s login and authorization page, then returns to your MCP client automatically. Sign in with the same Xingzhe account used in the mobile app. No separate setup page or owner key is required.

```text
ChatGPT → MCP authorization → Xingzhe login and consent
        ← MCP authorization code ← verified Xingzhe account
```

For ChatGPT, use a custom MCP app with OAuth, enter the client credentials above, and copy its exact callback URL into `MCP_REDIRECT_URIS`. Availability and setup are described in [OpenAI's authentication guide](https://developers.openai.com/apps-sdk/build/auth).

## Deploy to Vercel

Import your repository into Vercel with the FastAPI framework preset. [`app.py`](app.py) is the entrypoint; [`vercel.json`](vercel.json) configures the function. Dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

Connect your Supabase integration to this Vercel project and supply the variables from `.env.example` as server-side environment variables. If the integration supplies `POSTGRES_URL`, omit `DATABASE_URL`. Do not use a Supabase public API key as a database password.

Use a stable HTTPS production domain for `PUBLIC_URL`, update both providers' callback settings, then redeploy. Check `/ready` before connecting Xingzhe. MCP clients must be able to reach the OAuth metadata and `/mcp` without a Vercel login wall. Application OAuth still protects the activity data.

MCP uses stateless Streamable HTTP. Authorization state and encrypted tokens live in Supabase, so requests do not need to return to the same Vercel instance. Put preview deployments on a separate database and encryption key if you enable OAuth there.

## Access and storage

The service requests only Xingzhe's `read` scope. MCP uses a separate OAuth flow with PKCE, short-lived access tokens, rotating refresh tokens, and account-bound authorization. Xingzhe credentials and tokens are never returned to MCP clients.

The private table has RLS enabled and no public policies. It is accessed by the server through PostgreSQL, not Supabase's Data API. Supabase may report an informational “RLS enabled, no policy” notice; denying Data API access is intentional. Keep `xingzhe` out of exposed schemas.

The server verifies your account ID through Xingzhe’s profile API and binds MCP tokens to that identity. Tool arguments cannot select another account. Reconnecting one account does not change another account’s authorization. Multiple users can share a deployment; each MCP connection accesses one authorized Xingzhe account.

`POST /disconnect` with an MCP bearer token deletes that account’s stored Xingzhe tokens and all its MCP grants. Other accounts remain connected. `/revoke` revokes only the presented MCP grant. To revoke the upstream application's authorization itself, use Xingzhe's account settings. Losing or replacing `ENCRYPTION_KEY` makes existing records unreadable; back it up separately from the database.

`GET /health` checks process liveness. `GET /ready` checks configuration and storage. Missing or invalid configuration returns 503 on service routes. REST equivalents are `/api/activities` and `/api/activities/{id}` and require a valid MCP access token. Avoid recording authorization headers or callback query strings in logs.

Upgrading from the single-owner version requires running the new migration. It clears legacy authorization records in `xingzhe.records`; every user must reconnect. Remove the unused `ADMIN_KEY` environment variable.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv build
```

PostgreSQL integration tests cover the chained OAuth flow, concurrent multi-account MCP calls, account isolation, revocation, persistence, encryption, and concurrent token exchange. Set `TEST_DATABASE_URL` to an isolated database whose name ends in `_test`. Tests recreate its `xingzhe` schema. Without that variable, database tests are skipped. CI starts PostgreSQL and runs the full suite. Xingzhe's external API is mocked in tests.

Application code lives in [`src/xingzhe_mcp/`](src/xingzhe_mcp/). Add dependencies with `uv add` and commit `uv.lock` with `pyproject.toml`.

## Contributing

Issues and pull requests are welcome. Keep changes focused and both READMEs in sync. Use synthetic activity data in tests; never commit account credentials or personal ride records.

## License

[MIT](LICENSE). Not affiliated with Xingzhe.
