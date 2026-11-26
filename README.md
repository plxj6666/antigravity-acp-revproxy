# Antigravity ACP Gateway

[MIT 许可证](LICENSE) · [安全说明](SECURITY.md)

通过官方 `agy acp` 安全反代 Antigravity：客户端使用网关密钥调用熟悉的 HTTP API，**不需要拿到 Antigravity 账号的 OAuth 凭据**。适合在自己的机器上接入现有 AI 客户端，或为已获授权的团队搭建受控入口。本项目为第三方适配器，不是 Google 官方服务，也不提供账号、额度或公共代理。

```text
本机客户端 ── 网关密钥 ──▶ 127.0.0.1 上的 HTTP 网关 ── stdio ──▶ agy acp --sandbox
团队部署   ── HTTPS ──▶ Nginx ── Unix socket ──▶ 会话路由器 ──▶ 独立账号 worker
```

## “安全反代”指什么？

- **凭据分离。** 上游由官方 CLI 登录；客户端只使用可独立更换的网关密钥。多账号部署使用独立系统用户、HOME 和受权限保护的 Unix socket。管理面板另设管理员认证，不使用推理密钥代替管理员密码。
- **工具在客户端执行。** 网关校验客户端声明的函数和参数，把调用返回给客户端，由客户端决定是否执行；不会把客户端的 `bash` 或 `write` 直接当作服务器命令。未获允许的 ACP 权限请求会被拒绝。
- **会话与租户绑定。** 工具结果只能续接到匹配的会话；多槽路由不会把进行中的调用静默重试到另一个账号。不要给互不信任的用户分发同一密钥。
- **默认不开公网和原生调试日志。** 单机模式只监听回环地址。结构化请求指标不记录提示词或工具正文。上游 CLI 仍可能保存会话；`--sandbox` 不等于虚拟机隔离，更不代表零留存。部署前请阅读[安全边界](SECURITY.md)。

## 五分钟本地启动

需要 macOS 或 Linux、Python **3.11+**、可用的 Antigravity 账号，以及支持 ACP 的[官方 CLI](https://antigravity.google/docs/cli/acp/)。在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 首次联网准备 tiktoken 的 o200k_base 词表；不要在只读服务账户启动时才下载
TIKTOKEN_CACHE_DIR="$PWD/vocab" .venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
agy acp --login  # 已登录可跳过
export GATEWAY_API_KEY="$(openssl rand -hex 32)"
PORT=8787 .venv/bin/python gateway.py
```

让客户端通过安全方式读取**同一个** `GATEWAY_API_KEY`，不要重新生成。先验证模型列表，再发送一次流式请求：

```sh
BASE=http://127.0.0.1:8787
curl -fsS "$BASE/v1/models" -H "Authorization: Bearer $GATEWAY_API_KEY"
curl -fsSN "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"antigravity-default","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

`antigravity-default` 使用 CLI 默认模型。需要指定模型时，请从实际返回的 ID 中选择；HTTP 协议的名称不决定上游模型。OpenAI SDK 的 `base_url` 为 `http://127.0.0.1:8787/v1`。

## 支持哪些接口？

| 客户端协议 | 主要生成接口 |
|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Gemini REST | `POST /v1beta/models/{id}:generateContent` 或 `:streamGenerateContent` |

四种协议共享同一条 ACP 推理链路，支持文本、流式输出、内联图片及客户端工具续接，**不是四家官方 API 的完整实现**。不会代为下载图片 URL，不提供永久 Responses 存储，不保证上游缓存命中。额度面板是账号侧的额度快照，不是逐请求账单。

## 文档与验证

- [客户端配置](CLIENTS.md)：Codex CLI、Claude Code、Gemini CLI、Antigravity CLI。
- [协议示例与限制](docs/protocols.md)：鉴权头、SSE、图片与请求大小、工具策略、usage 口径。
- [部署说明](deploy/README.md)：HTTPS、管理员入口、Unix socket、账号槽位与会话路由。
- [安全与问题报告](SECURITY.md)：信任边界、日志和凭据处理。

完成依赖和词表准备后，可运行离线单测；不需要真实账号、云端模型或生产密钥：

```sh
.venv/bin/python -Werror -m unittest -v
```
