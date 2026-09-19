# Xingzhe MCP

English · [简体中文](README.zh-CN.md)

Bring your [Xingzhe](https://www.imxingzhe.com/) rides into conversations with AI assistants. Query activities, review a ride, and compare cadence, heart rate, or power when those measurements are available.

Python · FastAPI · MCP · Supabase · Vercel. Managed with [uv](https://docs.astral.sh/uv/).

## Why this project?

Your rides already live in the mainland China Xingzhe app. This service connects to its official API for read-only access to activities, routes, and upload history. One deployment supports multiple Xingzhe accounts. Each user authorizes their own account when connecting an MCP client.

| Tool | What it does |
| --- | --- |
| `get_profile` | Returns the authenticated account’s stable ID and current Xingzhe nickname. |
| `list_activities` | Lists activities with pagination and optional start/end timestamps. |
| `get_activity` | Returns activity totals and sensor summaries, without individual samples. |
| `get_activity_stream` | Returns aligned timestamp and sensor arrays with field selection and pagination. |
| `list_my_routes` | Lists routes created by the authenticated account. |
| `list_collected_routes` | Lists the account's collected routes. |
| `get_route` | Reads route navigation, elevation, turns, and waypoints. |
| `get_route_gpx` | Returns a route's GPX as text. |
| `list_uploads` | Lists the account's activity upload history. |

Timestamps used as filters are Unix milliseconds. Distance is in meters and duration in seconds. Other fields retain the upstream values; missing measurements are not inferred. Use `get_activity_stream` for individual samples.

## Getting started

You need a [Xingzhe developer application](https://www.imxingzhe.com/home/#/settings/api), a Supabase project, and an MCP client that supports OAuth dynamic client registration.

```sh
uv sync --locked
cp .env.example .env
```

1. Run the SQL in [`supabase/migrations/`](supabase/migrations/) once in your Supabase SQL editor, or apply it through your existing migration workflow. It creates a private `xingzhe` schema.
2. Fill in `.env`. Use the **transaction pooler** connection string from Supabase's Connect dialog for `DATABASE_URL`, with `sslmode=require`. Keep the actual pooler host from the dashboard. `POSTGRES_URL` is also accepted, so the Vercel integration can supply it directly.
3. Generate `ENCRYPTION_KEY` using the commands in [`.env.example`](.env.example). Keep the encryption key stable across deployments.
4. Set your Xingzhe application's callback to `PUBLIC_URL/oauth/xingzhe/callback`. For local development, that is `http://localhost:8000/oauth/xingzhe/callback`.

```sh
uv run uvicorn app:app --reload --no-access-log
```

Add `PUBLIC_URL/mcp` to your MCP client and choose OAuth. Client registration happens automatically; no client ID, secret, or callback configuration is needed. Confirm the requesting client and return address, authorize in Xingzhe, and you will return to your MCP client. Sign in with the account used in the mobile app.

Your browser remembers this confirmation for 30 days. Reconnecting the same client, return address and approved scopes goes directly to Xingzhe. A new client, additional permissions, cleared cookies or expired approval requires confirmation again. If you switch Xingzhe accounts during a remembered flow, restart the connection to confirm the new account. Remembered consent uses an encrypted, HttpOnly cookie; HTTPS deployments also use Secure and a host-only cookie prefix.

```text
ChatGPT → MCP authorization → Xingzhe login and consent
        ← MCP authorization code ← verified Xingzhe account
```

For ChatGPT, create a custom app, enter the MCP URL, leave OAuth selected, and connect without opening advanced OAuth settings. `get_profile` supplies the Xingzhe nickname for account labels using OpenAI's profile-tool metadata. ChatGPT controls when labels refresh and any user-defined nickname overrides. See [OpenAI's authentication guide](https://developers.openai.com/plugins/build/auth).

## Deploy to Vercel

Import your repository into Vercel with the FastAPI framework preset. [`app.py`](app.py) is the entrypoint; [`vercel.json`](vercel.json) configures the function. Dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

Connect your Supabase integration to this Vercel project and supply the variables from `.env.example` as server-side environment variables. If the integration supplies `POSTGRES_URL`, omit `DATABASE_URL`. Do not use a Supabase public API key as a database password.

Use a stable HTTPS production domain for `PUBLIC_URL`, update the Xingzhe application's callback, then redeploy. Check `/ready` before connecting Xingzhe. MCP clients must be able to reach the OAuth metadata and `/mcp` without a Vercel login wall. Application OAuth still protects the activity data.

MCP uses stateless Streamable HTTP. Authorization state and encrypted tokens live in Supabase, so requests do not need to return to the same Vercel instance. Put preview deployments on a separate database and encryption key if you enable OAuth there.

## Activity streams

`get_activity_stream` reads the authorized user's samples. Use `fields` to select cadence, heart rate (`heartrate`), location, speed, distance, altitude, power, temperature, or left/right balance. Timestamps are always included. Omit `fields` to return all upstream streams.

Each array index represents the same sample. `limit` defaults to 500 and accepts 1 to 1,000; pass `next_offset` as `offset` to continue. `total_points` describes the whole activity. Empty sensor arrays remain empty and appear in `unavailable_streams`; missing readings are never filled with zeros.

Activity detail timestamps use Unix milliseconds, while verified stream timestamps use Unix seconds. Values and sampling intervals are preserved, including zero readings, duplicate timestamps and gaps. Other stream units are not normalized. A sample mean, or a mean excluding zero cadence, need not match the app's aggregate. A zero cadence summary does not prove the stream has no cadence. `get_activity` preserves the upstream summary instead of replacing it with a calculated value.

The live API exposes both `/activities/` and `/workout/` paths. This service uses `/activities/{id}/stream/`. Its POST request reads data with existing `read` authorization; it does not upload or modify an activity. Each page fetches the complete upstream response, capped at 20,000,000 bytes, then selects fields and slices samples. Lap data and FIT downloads are not provided by this tool.

## Routes and upload history

Route and upload lists accept `limit` from 1 to 20 and a nonnegative `offset`; use `next_offset` to continue. Shared or collected routes need not belong to the current user. Route detail and GPX requests use the current account's credentials and respect Xingzhe's visibility rules.

GPX downloads return a filename and UTF-8 XML text, limited to **1,000,000 bytes** per file. DTD/entity declarations are rejected. Tools do not fetch arbitrary file URLs.

The [route guide](https://developer.imxingzhe.com/docs/openapi/routes/) differs from the authenticated [live Swagger schema](https://www.imxingzhe.com/openapi/doc/?format=openapi). This implementation uses the verified `/routes/{id}/pro/` navigation endpoint; `/raw/` is not available on the live server.

## Access and storage

All MCP tools are read-only. MCP requests `activities:read`, and the upstream Xingzhe authorization requests `read`. OAuth supports PKCE, short-lived access tokens, rotating refresh tokens, and account-bound authorization. Xingzhe credentials and tokens are never returned to MCP clients.

The private table has RLS enabled and no public policies. It is accessed by the server through PostgreSQL, not Supabase's Data API. Supabase may report an informational “RLS enabled, no policy” notice; denying Data API access is intentional. Keep `xingzhe` out of exposed schemas.

The server verifies your account ID through Xingzhe’s profile API and binds MCP tokens to that identity. Tool arguments cannot select another account. Reconnecting one account does not change another account’s authorization. Multiple users can share a deployment; each MCP connection accesses one authorized Xingzhe account.

`POST /disconnect` with an MCP bearer token deletes that account’s stored Xingzhe tokens and all its MCP grants. Other accounts remain connected. `/revoke` revokes only the presented MCP grant. To revoke the upstream application's authorization itself, use Xingzhe's account settings. Losing or replacing `ENCRYPTION_KEY` makes existing records unreadable; back it up separately from the database.

`GET /health` checks process liveness. `GET /ready` checks configuration and storage. Missing or invalid configuration returns 503 on service routes. REST equivalents are `/api/activities` and `/api/activities/{id}` and require a valid MCP access token. Avoid recording authorization headers or callback query strings in logs.

Apply new migrations before deploying upgrades. Dynamic OAuth clients and their credentials are stored encrypted alongside grants, without automatic expiry. Registration only accepts HTTPS callbacks or HTTP loopback callbacks; each authorization validates an exact registered URI and requires browser-bound consent. Do not delete registered clients while their connections are in use.

When upgrading from environment-configured MCP credentials, reconnect the ChatGPT app using automatic registration, then remove `MCP_CLIENT_ID`, `MCP_CLIENT_SECRET`, and `MCP_REDIRECT_URIS`. The dynamic-client migration preserves existing account data. The earlier single-owner migration clears legacy grants and requires reauthorization.

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
