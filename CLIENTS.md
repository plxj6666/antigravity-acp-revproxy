# CLI 接入（文本和流式输出）

以下命令假设 `GATEWAY_API_KEY` 已通过本机密钥管理器注入。不要把密钥写进仓库。用 `/v1/models` 中实际存在的模型 ID。

## Codex CLI

```sh
codex exec -m antigravity-default -c 'model_provider="gateway"' \
  -c 'model_providers.gateway.name="ACP Gateway"' \
  -c 'model_providers.gateway.base_url="http://127.0.0.1:8787/v1"' \
  -c 'model_providers.gateway.env_key="GATEWAY_API_KEY"' 'hi'
```

## Claude Code

```sh
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY="$GATEWAY_API_KEY" claude --model antigravity-default -p 'hi'
```

## Gemini CLI

选择 Gemini API key 认证，不是 Google OAuth。

```sh
GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787 GEMINI_API_KEY="$GATEWAY_API_KEY" gemini -m antigravity-default -p 'hi'
```

## Antigravity CLI 作为客户端

在客户端 `~/.gemini/antigravity-cli/settings.json` 中设置 `"modelProvider":"gemini"`（合并到现有设置）。不要改网关上游服务账号的配置。

```sh
GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787 GEMINI_API_KEY="$GATEWAY_API_KEY" agy --model antigravity-default -p 'hi'
```

当前工作区支持四协议的内联图片、可选或强制函数、同槽位挂起式多工具续接、模型实际发出的公开摘要流，以及基于上游结算字段的 usage 映射。客户端按自己的权限执行工具；ACP 不提供单次函数参数生成过程的分片，网关收到完整参数后才发兼容事件。没有结算数据时不能据占用量推算账单。Responses 的 previous_response_id 不是长期存储。

多槽位部署通过 call/response ID 或会话指纹亲和，无需客户端固定 TCP 连接。保留完整工具 ID，不要在 409/503 后盲目重试有副作用的操作。缓存命中由提供方决定，不支持显式缓存创建或跨账号共享。以上能力须部署对应版本后才在线上可用。
