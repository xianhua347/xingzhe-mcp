# Xingzhe MCP

[English](README.md) · 简体中文

让 AI 助手读取你的[行者](https://www.imxingzhe.com/)骑行记录。查询运动、回顾骑行，并在数据可用时比较踏频、心率和功率。

Python · FastAPI · MCP · Supabase · Vercel，使用 [uv](https://docs.astral.sh/uv/) 管理项目。

## 为什么做这个项目？

骑行记录已经保存在中国大陆行者 App 中。这个服务通过官方 API 提供运动记录、路书和上传记录的只读查询工具。一个部署支持多个行者账号，每位用户在连接 MCP 客户端时授权自己的账号。

| 工具 | 用途 |
| --- | --- |
| `get_profile` | 返回当前授权账号的固定 ID 和最新行者昵称。 |
| `list_activities` | 分页查询运动记录，可指定开始和结束时间戳。 |
| `get_activity` | 获取单次运动总量和传感器汇总，不包含逐点数据。 |
| `get_activity_stream` | 按字段和分页读取时间戳及传感器序列。 |
| `list_my_routes` | 查询当前账号创建的路书。 |
| `list_collected_routes` | 查询当前账号收藏的路书。 |
| `get_route` | 获取路书导航、高程、转向和途经点数据。 |
| `get_route_gpx` | 以文本返回路书 GPX。 |
| `list_uploads` | 查询当前账号的运动上传记录。 |

查询时间使用 Unix 毫秒时间戳，距离单位为米，时长单位为秒。其他字段保留上游原值，不推测缺失的测量值。逐点数据请使用 `get_activity_stream`。

## 快速开始

需要一个[行者开发者应用](https://www.imxingzhe.com/home/#/settings/api)、一个 Supabase 项目，以及支持 OAuth 动态客户端注册的 MCP 客户端。

```sh
uv sync --locked
cp .env.example .env
```

1. 在 Supabase SQL 编辑器中执行一次 [`supabase/migrations/`](supabase/migrations/) 中的 SQL，或通过现有迁移流程应用。它会创建私有的 `xingzhe` schema。
2. 填写 `.env`。从 Supabase 的 Connect 面板复制 **Transaction pooler** 连接串作为 `DATABASE_URL`，启用 `sslmode=require`，保留面板中的实际连接池域名。也支持 Vercel 集成注入的 `POSTGRES_URL`。
3. 按 [`.env.example`](.env.example) 中的命令，生成 `ENCRYPTION_KEY`。重新部署时保留原加密密钥。
4. 将行者应用的回调地址设置为 `PUBLIC_URL/oauth/xingzhe/callback`。本地开发时为 `http://localhost:8000/oauth/xingzhe/callback`。

```sh
uv run uvicorn app:app --reload --no-access-log
```

在 MCP 客户端添加 `PUBLIC_URL/mcp`，选择 OAuth。客户端会自动注册，无需填写客户端 ID、密钥或回调地址。确认申请方和返回地址后，进入行者授权，完成后自动返回 MCP 客户端。请登录与手机 App 相同的行者账号。

浏览器会记住此次确认 30 天。同一客户端、回调地址和已批准权限再次连接时，直接跳转行者。新客户端、增加权限、清除 Cookie 或确认过期后，需要再次确认。如果在免确认流程中切换行者账号，请重新发起连接以确认新账号。确认记录使用加密的 HttpOnly Cookie；HTTPS 部署还启用 Secure 和仅限当前主机的 Cookie 前缀。

```text
ChatGPT → MCP 授权 → 行者登录并确认授权
        ← MCP 授权码 ← 验证行者账户身份
```

在 ChatGPT 中创建自定义应用，填写 MCP 地址，保留 OAuth 选项，即可连接，不需要打开高级 OAuth 配置。`get_profile` 按 OpenAI 的账户资料协议提供行者昵称，供连接列表显示。名称刷新时机以及手动昵称的优先级由 ChatGPT 控制。参见 [OpenAI 授权文档](https://developers.openai.com/plugins/build/auth)。

## 部署到 Vercel

将仓库导入 Vercel，选择 FastAPI 框架预设。[`app.py`](app.py) 是入口，[`vercel.json`](vercel.json) 配置函数。依赖声明在 `pyproject.toml`，版本锁定在 `uv.lock`。

将 Supabase 集成关联到这个 Vercel 项目，并将 `.env.example` 中的变量配置为服务端环境变量。如果集成已注入 `POSTGRES_URL`，可以不设置 `DATABASE_URL`。Supabase 的公开 API Key 不能作为数据库密码使用。

将 `PUBLIC_URL` 设置为固定的 HTTPS 生产域名，更新行者应用的回调地址，然后重新部署。连接行者前先检查 `/ready`。MCP 客户端必须能直接访问 OAuth 元数据和 `/mcp`，不能被 Vercel 登录保护拦截；运动数据由应用自身的 OAuth 保护。

MCP 使用无状态 Streamable HTTP。授权状态和加密令牌保存在 Supabase，后续请求可以由不同 Vercel 实例处理。如果在预览部署中启用 OAuth，请使用独立数据库和加密密钥。

## 运动采样序列

`get_activity_stream` 读取当前授权用户的运动采样点。通过 `fields` 选择踏频 `cadence`、心率 `heartrate`、位置 `location`、速度、距离、海拔、功率、温度或左右平衡。始终返回 `timestamp`；省略 `fields` 时返回上游全部序列。

各数组相同下标对应同一个采样点。`limit` 默认 500，范围为 1 到 1,000；将 `next_offset` 作为下一次的 `offset`。`total_points` 为整条活动的采样点数。缺失的传感器序列保持空数组，并列入 `unavailable_streams`，不会用零补齐。

运动详情时间戳使用 Unix 毫秒，已验证的 stream 时间戳使用 Unix 秒。保留原始数值和采样间隔，包括零值、重复时间戳和时间缺口；其他 stream 字段不做单位换算。逐点平均值或排除零踏频后的平均值，不一定等于 App 汇总。详情中的踏频为零，也不代表采样序列没有踏频。`get_activity` 保留上游汇总，不用计算结果覆盖。

线上接口同时提供 `/activities/` 和 `/workout/` 路径。本项目使用 `/activities/{id}/stream/`，通过 POST 读取数据，沿用已有 `read` 授权，不上传或修改运动记录。每次分页请求都会获取完整上游响应，上限为 20,000,000 字节，然后选择字段并截取采样点。此工具不提供记圈数据或 FIT 下载。

## 路书与上传记录

路书和上传列表的 `limit` 范围为 1–20，`offset` 为非负数，使用 `next_offset` 翻页。收藏或共享路书可能属于其他用户，导航和 GPX 请求始终携带当前账号的凭证，由行者控制可见范围。

下载 GPX 会返回文件名和 UTF-8 XML 文本，每个文件上限为 **1,000,000 字节**。不允许 DTD 或实体声明，工具不会下载任意文件 URL。

[路书文档](https://developer.imxingzhe.com/docs/openapi/routes/)与需要授权的[线上 Swagger](https://www.imxingzhe.com/openapi/doc/?format=openapi)存在差异。本项目采用已验证可用的 `/routes/{id}/pro/` 导航接口，线上 `/raw/` 不存在。

## 访问与存储

全部 MCP 工具均为只读。MCP 仅申请 `activities:read`，行者授权仅申请 `read`。OAuth 支持 PKCE、短期访问令牌、刷新令牌轮换和按账户绑定授权。行者凭证与令牌不会返回给 MCP 客户端。

私有表启用 RLS，不设置公开访问策略。服务通过 PostgreSQL 访问它，不使用 Supabase Data API。Supabase 可能提示“已启用 RLS，但没有策略”，这是阻止 Data API 访问的预期配置。不要将 `xingzhe` 加入公开 schema 列表。

服务通过行者个人信息接口验证用户 ID，并将 MCP 令牌绑定到该账户。工具参数不能指定其他账户。一个部署可供多个用户使用，每个 MCP 连接读取其授权的一个行者账号；重新授权不会切换其他用户的账户。

携带 MCP Bearer 令牌调用 `POST /disconnect`，可删除当前账户保存的行者令牌及全部 MCP 授权，其他账户不受影响。`/revoke` 只撤销对应的一组 MCP 令牌。若要撤销行者平台上的应用授权，请在行者账号设置中操作。丢失或替换 `ENCRYPTION_KEY` 会导致现有记录无法解密，请将它与数据库分别备份。

`GET /health` 检查进程是否运行，`GET /ready` 检查配置和存储。配置缺失或无效时，业务路由返回 503。REST 接口为 `/api/activities` 和 `/api/activities/{id}`，同样需要有效的 MCP 访问令牌。日志中应避免记录授权请求头和回调地址的查询参数。

升级前先执行新增迁移。动态注册的客户端和凭证加密保存在数据库中，不自动过期。注册只接受 HTTPS 或本机回环 HTTP 回调；每次授权严格匹配已注册地址，并要求浏览器内确认。连接仍在使用时，不要删除对应的客户端记录。

从环境变量配置 MCP 凭证的版本升级时，在 ChatGPT 中使用自动注册重新连接，再移除 `MCP_CLIENT_ID`、`MCP_CLIENT_SECRET` 和 `MCP_REDIRECT_URIS`。动态客户端迁移会保留已有账户数据；更早的单账户迁移会清除旧授权，需要重新连接。

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
