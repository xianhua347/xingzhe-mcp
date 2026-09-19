# Xingzhe MCP

[English](README.md) · 简体中文

让 AI 助手读取你的[行者](https://www.imxingzhe.com/)骑行记录。查询运动、回顾骑行，并在数据可用时比较踏频、心率和功率。

Python · FastAPI · MCP · Supabase · Vercel，使用 [uv](https://docs.astral.sh/uv/) 管理项目。

## 为什么做这个项目？

骑行记录已经保存在中国大陆行者 App 中。这个服务通过官方 API 读取记录，提供两个只读 MCP 工具。一个部署支持多个行者账号，每位用户在连接 MCP 客户端时授权自己的账号。

| 工具 | 用途 |
| --- | --- |
| `list_activities` | 分页查询运动记录，可指定开始和结束时间戳。 |
| `get_activity` | 获取单次运动详情，包括行者提供的传感器汇总数据。 |

查询时间使用 Unix 毫秒时间戳，距离单位为米，时长单位为秒。其他字段保留上游原值，不推测缺失的测量值。运动 API 不提供逐秒踏频序列或 FIT 文件下载。

## 快速开始

需要一个[行者开发者应用](https://www.imxingzhe.com/home/#/settings/api)、一个 Supabase 项目，以及支持配置 OAuth 客户端 ID 和密钥的 MCP 客户端。

```sh
uv sync --locked
cp .env.example .env
```

1. 在 Supabase SQL 编辑器中执行一次 [`supabase/migrations/`](supabase/migrations/) 中的 SQL，或通过现有迁移流程应用。它会创建私有的 `xingzhe` schema。
2. 填写 `.env`。从 Supabase 的 Connect 面板复制 **Transaction pooler** 连接串作为 `DATABASE_URL`，启用 `sslmode=require`，保留面板中的实际连接池域名。也支持 Vercel 集成注入的 `POSTGRES_URL`。
3. 按 [`.env.example`](.env.example) 中的命令，分别生成 `ENCRYPTION_KEY` 和 `MCP_CLIENT_SECRET`。重新部署时保留原加密密钥。
4. 将行者应用的回调地址设置为 `PUBLIC_URL/oauth/xingzhe/callback`。本地开发时为 `http://localhost:8000/oauth/xingzhe/callback`。
5. 将 MCP 客户端提供的精确回调地址填入 `MCP_REDIRECT_URIS`，格式为 JSON 数组。这与行者回调地址不同，不接受通配符。

```sh
uv run uvicorn app:app --reload --no-access-log
```

在 MCP 客户端添加 `PUBLIC_URL/mcp`，填写 `MCP_CLIENT_ID` 和 `MCP_CLIENT_SECRET`。连接时会自动跳转至行者登录和授权页面，完成后返回 MCP 客户端。请登录与手机 App 相同的行者账号，无需提前访问连接页面或输入所有者密钥。

```text
ChatGPT → MCP 授权 → 行者登录并确认授权
        ← MCP 授权码 ← 验证行者账户身份
```

在 ChatGPT 中使用带 OAuth 的自定义 MCP 应用，填写上述客户端凭证，并把它提供的精确回调地址写入 `MCP_REDIRECT_URIS`。功能可用范围与配置方法参见 [OpenAI 授权文档](https://developers.openai.com/apps-sdk/build/auth)。

## 部署到 Vercel

将仓库导入 Vercel，选择 FastAPI 框架预设。[`app.py`](app.py) 是入口，[`vercel.json`](vercel.json) 配置函数。依赖声明在 `pyproject.toml`，版本锁定在 `uv.lock`。

将 Supabase 集成关联到这个 Vercel 项目，并将 `.env.example` 中的变量配置为服务端环境变量。如果集成已注入 `POSTGRES_URL`，可以不设置 `DATABASE_URL`。Supabase 的公开 API Key 不能作为数据库密码使用。

将 `PUBLIC_URL` 设置为固定的 HTTPS 生产域名，更新两套授权流程的回调地址，然后重新部署。连接行者前先检查 `/ready`。MCP 客户端必须能直接访问 OAuth 元数据和 `/mcp`，不能被 Vercel 登录保护拦截；运动数据由应用自身的 OAuth 保护。

MCP 使用无状态 Streamable HTTP。授权状态和加密令牌保存在 Supabase，后续请求可以由不同 Vercel 实例处理。如果在预览部署中启用 OAuth，请使用独立数据库和加密密钥。

## 访问与存储

服务只申请行者的 `read` 权限。MCP 使用独立的 OAuth 流程，支持 PKCE、短期访问令牌、刷新令牌轮换和按账户绑定授权。行者凭证与令牌不会返回给 MCP 客户端。

私有表启用 RLS，不设置公开访问策略。服务通过 PostgreSQL 访问它，不使用 Supabase Data API。Supabase 可能提示“已启用 RLS，但没有策略”，这是阻止 Data API 访问的预期配置。不要将 `xingzhe` 加入公开 schema 列表。

服务通过行者个人信息接口验证用户 ID，并将 MCP 令牌绑定到该账户。工具参数不能指定其他账户。一个部署可供多个用户使用，每个 MCP 连接读取其授权的一个行者账号；重新授权不会切换其他用户的账户。

携带 MCP Bearer 令牌调用 `POST /disconnect`，可删除当前账户保存的行者令牌及全部 MCP 授权，其他账户不受影响。`/revoke` 只撤销对应的一组 MCP 令牌。若要撤销行者平台上的应用授权，请在行者账号设置中操作。丢失或替换 `ENCRYPTION_KEY` 会导致现有记录无法解密，请将它与数据库分别备份。

`GET /health` 检查进程是否运行，`GET /ready` 检查配置和存储。配置缺失或无效时，业务路由返回 503。REST 接口为 `/api/activities` 和 `/api/activities/{id}`，同样需要有效的 MCP 访问令牌。日志中应避免记录授权请求头和回调地址的查询参数。

从单账户版本升级时需要执行新增迁移。迁移会清除 `xingzhe.records` 中的旧授权记录，所有用户需重新连接，并移除已停用的 `ADMIN_KEY` 环境变量。

## 开发

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv build
```

PostgreSQL 集成测试覆盖完整 OAuth 跳转、多账户并发 MCP 调用、账户隔离、撤销授权、持久化、加密和并发令牌交换。将 `TEST_DATABASE_URL` 指向数据库名以 `_test` 结尾的独立测试库，测试会重建其中的 `xingzhe` schema。未设置该变量时跳过数据库测试。CI 会启动 PostgreSQL 并运行完整测试，测试中的行者外部接口使用模拟响应。

应用代码位于 [`src/xingzhe_mcp/`](src/xingzhe_mcp/)。使用 `uv add` 添加依赖，将 `uv.lock` 与 `pyproject.toml` 一起提交。

## 参与贡献

欢迎提交 Issue 和 Pull Request。保持修改范围集中，并同步维护中英文 README。测试使用合成数据，不要提交账号凭证或个人运动记录。

## 许可证

[MIT](LICENSE)。本项目与行者官方无关联。
