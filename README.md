# Xingzhe MCP

English · [简体中文](README.zh-CN.md)

An open-source project for bringing your [Xingzhe](https://www.imxingzhe.com/) cycling data to AI assistants through the Model Context Protocol.

Built with Python and FastAPI. Managed with uv. Designed for self-hosting.

## Why this project?

Your rides already live in Xingzhe. The goal is to make that same data useful in a conversation: review a ride, compare cadence across sessions, or look back at a month of cycling.

The focus is the mainland China Xingzhe app and its official APIs. Keep the service small, use your own account, and make it easy to run and contribute to.

## Getting started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run these commands from the repository root:

```sh
uv sync --locked
uv run uvicorn app:app --reload
```

uv installs the project's Python version and dependencies. Open [localhost:8000/docs](http://localhost:8000/docs) to explore the HTTP API, or check [/health](http://localhost:8000/health) to verify the server is running.

## Development

Use Python type annotations throughout. mypy runs in strict mode, Ruff handles linting and formatting, and pytest runs the tests.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Application code lives in [`src/xingzhe_mcp/`](src/xingzhe_mcp/). The root [`app.py`](app.py) exports the FastAPI application for Uvicorn and [Vercel](https://vercel.com/docs/frameworks/backend/fastapi).

Add dependencies with `uv add` or `uv add --dev`, and commit the updated `uv.lock` alongside `pyproject.toml`.

## Contributing

Bug reports and pull requests are welcome. For larger changes, open an issue first so we can agree on the scope. Keep changes focused, test the behavior you change, and keep the English and Chinese READMEs in sync.

Use synthetic data in tests and examples. Never include account credentials, access tokens, or personal ride records in a pull request.

## License

[MIT](LICENSE). This project is not affiliated with Xingzhe.
