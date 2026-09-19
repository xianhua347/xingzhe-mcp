# Xingzhe MCP

[English](README.md) · 简体中文

一个开源项目，旨在通过 Model Context Protocol 将你的[行者](https://www.imxingzhe.com/)骑行数据接入 AI 助手。

使用 Python 和 FastAPI 构建，采用 uv 管理，面向自托管使用。

## 为什么做这个项目？

骑行记录已经在行者里了。我们希望这些数据也能用于对话：复盘一次骑行、比较几次骑行的踏频，或者回顾一个月的骑行情况。

项目面向中国大陆行者 App，使用官方 API。保持服务简单，使用自己的账号，让部署和参与开发都更容易。

## 快速开始

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在仓库根目录运行：

```sh
uv sync --locked
uv run uvicorn app:app --reload
```

uv 会安装项目指定的 Python 版本和依赖。打开 [localhost:8000/docs](http://localhost:8000/docs) 查看 HTTP API，或访问 [/health](http://localhost:8000/health) 确认服务已启动。

## 开发

代码统一使用 Python 类型注解。mypy 以严格模式检查类型，Ruff 负责代码检查和格式化，pytest 运行测试。

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

应用代码位于 [`src/xingzhe_mcp/`](src/xingzhe_mcp/)。根目录的 [`app.py`](app.py) 导出 FastAPI 应用，供 Uvicorn 和 [Vercel](https://vercel.com/docs/frameworks/backend/fastapi) 使用。

使用 `uv add` 或 `uv add --dev` 添加依赖，并将更新后的 `uv.lock` 与 `pyproject.toml` 一同提交。

## 参与贡献

欢迎提交问题和 Pull Request。较大的改动请先开 Issue 讨论范围。保持改动集中，验证修改涉及的行为，并同步更新中英文 README。

测试和示例请使用合成数据。不要在 Pull Request 中包含账号凭据、访问令牌或个人骑行记录。

## 许可证

[MIT](LICENSE)。本项目与行者官方无隶属关系。
