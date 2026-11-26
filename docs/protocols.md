# HTTP 协议与兼容边界

一个本地端口提供四种 HTTP 协议的**文本与内联图片子集**。它们只是不同的请求/响应格式，均调用同一个 `agy acp` Agent 和账号实际可用的模型。例如，用 Anthropic 客户端也能选 Gemini 模型。这不是 Google、Anthropic 或 OpenAI 的完整 API 实现。

## 启动

macOS/Linux，Python 3.11+，已安装带 ACP 的 `agy`。升级旧版网关时停掉旧进程并安装新增依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
TIKTOKEN_CACHE_DIR="$PWD/vocab" .venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
agy acp --login  # 已登录跳过
export GATEWAY_API_KEY="$(openssl rand -hex 32)"  # 将此值安全配置到客户端；不要用 Google 密码
PORT=8787 .venv/bin/python gateway.py
```

只监听 `127.0.0.1`。可用 `AGY_BIN` 指定 CLI 绝对路径。下例假设同一个网关密钥已导入测试终端：

```sh
BASE=http://127.0.0.1:8787
AUTH="Authorization: Bearer $GATEWAY_API_KEY"
curl -sS "$BASE/v1/models" -H "$AUTH"
```

从结果挑模型 ID；`antigravity-default` 表示 CLI 默认模型。OpenAI SDK 的 `base_url` 包含 `/v1`；Anthropic SDK 和 Google Gen AI SDK 的 base URL 填主机及端口，不含 `/v1`。以下均为可独立执行的流式测试：

```sh
# 1. OpenAI Chat Completions：data 中的 choices[0].delta.content；以 [DONE] 结束
curl -sSN "$BASE/v1/chat/completions" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"model":"antigravity-default","messages":[{"role":"user","content":"解释交叉验证"}],"stream":true}'

# 2. OpenAI Responses：response.output_text.delta，最后 response.completed 或 response.incomplete
curl -sSN "$BASE/v1/responses" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"model":"antigravity-default","input":"解释交叉验证","stream":true}'

# 3. Anthropic Messages：content_block_delta，最后 message_stop
curl -sSN "$BASE/v1/messages" -H "x-api-key: $GATEWAY_API_KEY" -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' \
  -d '{"model":"antigravity-default","max_tokens":4096,"messages":[{"role":"user","content":"解释交叉验证"}],"stream":true}'

# 4. Gemini REST：candidates[0].content.parts[].text；无 alt=sse 时返回流式 JSON 数组
curl -sSN "$BASE/v1beta/models/antigravity-default:streamGenerateContent?alt=sse" -H "x-goog-api-key: $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"解释交叉验证"}]}]}'
```

**非流式**：前三种删除 `stream` 或设为 `false`；Gemini 使用 `:generateContent`。Gemini 生成接口也支持 `/v1/models/{id}:…`；Gemini 格式的模型发现使用 `GET /v1beta/models`。四种接口均接受 Bearer，Gemini 另支持 `x-goog-api-key` 和 `?key=`（推荐请求头，避免密钥出现在 URL 中）。

**流式输入的含义**：支持带 Content-Length 的普通 JSON，也支持 HTTP/1.1 chunked 分块上传同一份 JSON；网关不要求客户端预先知道长度。ACP 的 `session/prompt` 接收完整消息，因此上传结束且 JSON 验证通过后才开始生成。这不是双向实时语音或逐字触发推理。示例：

```sh
printf '%s' '{"input":"你好","stream":true}' | curl --http1.1 -sSN "$BASE/v1/responses" \
  -H "$AUTH" -H 'Content-Type: application/json' -H 'Transfer-Encoding: chunked' --data-binary @-
```

## 支持范围和限制

以下为当前工作区实现，并非线上多槽位发布声明。

- 四协议接受文本及内联 PNG/JPEG/WebP/GIF；不下载 URL，不接受音视频或 PDF。单图 ≤5 MiB、≤10 张、解码总计 ≤10 MiB；HTTP 请求 ≤12 MiB，非图片部分 ≤2 MiB。Base64 膨胀后可能先触及 HTTP 上限。冷请求超过 48,000 文本字节时使用请求级只读历史档案，图片不进入档案。
- 多槽位部署通过 Unix socket 路由器固定会话；结果 ID 直接回原槽位，离线不跨槽重试。安装见 [部署说明](../deploy/README.md)。四协议按上游结算数据映射 usage；ACP 不提供单个工具的参数生成分片。
- 工具由客户端执行。支持 none/auto/required/指定函数；强制模式不满足时同 ACP 会话最多纠偏一次，仍失败则返回 502 或流错误帧。等待客户端交付不占 1000 秒活跃生成预算，按 TOOL_WAIT_TIMEOUT 过期。
- Responses 支持 previous_response_id 和 store:true，限相同租户、原工作进程内的当前有效响应引用；不是永久存储或历史版本查询。工具结果只交付一次；不要盲目重试有副作用的调用。
- 四协议 SSE 按需发送上游公开摘要，不提供私有推理。Gemini/Anthropic 数字思考预算映射到模型可用档位。支持 JSON/Schema 输出约束；非流式校验，流式不保证完整 JSON。支持配置停用词；生成采样仍由提供方控制，不关闭上游安全过滤。
- 输出最多 100,000 token：按块 o200k_base 保守计数，若上游明确发出轮次 outputTokens 也施加限制，used/size 占用量不是生成量或计费依据。每槽至多 100 活跃、32 排队；100 并发不代表已通过负载测试。
- 这是带工具的 Agent，不是裸模型。网关不提供客户端文件或终端能力，拒绝原生工具权限，仅允许网关注册的桥接/只读历史工具并启用 CLI `--sandbox`；不等于完全禁用内置工具。CLI 可能保存会话或日志。团队使用时确认账号条款、组织授权和配额，在隔离的专用系统用户或容器下运行。共享密钥不提供成员级权限或审计。
- 远程连接用 `ssh -N -L 8787:127.0.0.1:8787 user@网关主机`，不要直接开放公网。需要不互信成员隔离或精确计费时，应使用官方模型服务。

参考：[Google Antigravity CLI ACP](https://antigravity.google/docs/cli/acp/)。
