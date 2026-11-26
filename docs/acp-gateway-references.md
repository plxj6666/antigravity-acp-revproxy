# 学生用 ACP 长会话网关：源码调研与上线门槛

范围：静态核对三个 GitHub 仓库源码，未运行其安装程序、未部署替代网关。本文的门槛是拟议验收，不是当前能力。

## 可参考的实现

| 项目（均为 MIT，链接固定到本次核对的提交） | 可借鉴机制 | 不宜照搬 |
|---|---|---|
| sujoshua/agy-gateway：[会话管理](https://github.com/sujoshua/agy-gateway/blob/9f3e671ad0e4c1b48d4ac7ee4ce095816a37ee25/internal/session/manager.go)、[MCP 桥](https://github.com/sujoshua/agy-gateway/blob/9f3e671ad0e4c1b48d4ac7ee4ce095816a37ee25/internal/bridge/bridge.go) | 按工具调用 ID 或历史前缀找到已有会话；限制会话数和 TTL；MCP 调用保持挂起，收到匹配的工具结果后只交付一次；区分运行时重启时的空闲和在途会话。 | 主要是 OpenAI Chat，并非现有四协议的直接替代。[运行时实测笔记](https://github.com/sujoshua/agy-gateway/blob/9f3e671ad0e4c1b48d4ac7ee4ce095816a37ee25/docs/runtime-notes.md)针对独立 agy-acp-server，不是我们部署的 agy CLI。不要移植运行时补丁或假定支持 HTTP MCP。 |
| openclaw/acpx：[恢复逻辑](https://github.com/openclaw/acpx/blob/3bc048964a4fd3ef1e4c3a683646ee7d926b8382/src/session/execution/session-management.ts)、[队列与租约](https://github.com/openclaw/acpx/tree/3bc048964a4fd3ef1e4c3a683646ee7d926b8382/src/session/queue) | 按能力选择 resume/load；会话有明确持有者、队列和租约。用于设计单会话串行执行和进程故障恢复。 | 是 ACP 客户端，不是学生多租户 HTTP 网关。恢复会话不代表能恢复正在执行的工具。 |
| vadim-vyb/acp-openai-gateway：[SessionStore](https://github.com/vadim-vyb/acp-openai-gateway/blob/8c5f278afd304810a93d436d5a9d3f230b18e0c4/src/acp_openai_gateway/sessions.py)、[测试](https://github.com/vadim-vyb/acp-openai-gateway/tree/8c5f278afd304810a93d436d5a9d3f230b18e0c4/tests) | Python 小实现，历史匹配后只发新增消息；可用于现有 aiohttp 网关的差分测试。 | 哈希归一化主要保留 role 和文本；不能表示完整工具参数、工具结果与分支语义，也不能充当租户授权。 |

## 与当前部署的对应

- `acp.py` 仍按请求启动/销毁 ACP 子进程，`formats.py` 将历史转成一条 prompt。相同用户界面会话不等于相同 ACP 会话。
- 对生产 lab1 另起临时 CLI 仅调用 `initialize`：协议 v1，`loadSession: true`，`sessionCapabilities` 有 `resume/load/close/list`，没有返回 `mcpCapabilities`。这是能力声明，不是验证过的跨进程工具续接；优先复用已在本部署跑通的 stdio MCP。
- 之前的生产测试在约 196–214 KB 出现正常 STOP 却无正文；256 KiB 是另一个独立的 HTTP 限制。会话复用节省上游重放，但不缩小兼容客户端 POST 的请求体，两个问题要分别解决。

## 推荐迭代顺序

1. 先让失败可见。请求 ID 对齐入口、会话管理器和 runtime；仅记事件类型、阶段、字节与错误，不记学生原文或凭证。无正文、无工具又无合理非文本终态，不生成成功 STOP；已开始流则返回协议内错误。记录首个真实事件与最终状态，不能用 HTTP 200 或心跳代替成功。
2. 在四种协议外观与 ACP 中间加会话管理层。每个学生有独立身份；显式随机会话 ID 优先。无法传 ID 的客户端仅在同租户、同会话配置内匹配完整规范化前缀，歧义则不复用；另开分支绝不能修改旧线程。账号轮转只用于分配新会话；记录并固定现有会话的账号槽位、工作目录、模型、工具配置与 ACP sessionId。同一会话同一时刻一个 active prompt。对不支持增量输入的客户端设置明确的 HTTP 预算、提前告警和压缩方案，不静默截断。
3. 用挂起的 MCP 调用替代“发现工具就销毁进程”。记录租户、会话、call ID、参数摘要、等待和交付状态；超时或取消后拒绝迟到结果。区分重复交付与重复执行，副作用结果不明时停止并提示核对，不自动重跑。账号失效时不要把活跃工具调用切给其他账号。
4. 独立测试槽位灰度：先验证空闲会话关闭进程后能恢复；再测已发首字后断流、已发工具后断流、提交结果后断流、网关重启。逐项定义可恢复/必须报告中断的边界。基于本机 RAM 实测为每学生限制活跃会话、并发、队列、空闲寿命，切勿把配置的 100 并发作为容量承诺。
5. 学生开放前验收：四协议各跑 100 轮、早中晚随机标记、跨越旧 256 KiB 边界的工具历史；两个学生用相同开头，绝不串话；write/edit 注入断线不重复执行；SSE 返回空成功次数为零；至少一次课堂规模并发演练并记录错误率、P95 首文本时间、峰值 RSS。新会话可切回旧后端；进行中会话不得透明迁移或重放。实测达标再逐步放量。
