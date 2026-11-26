# 多账号部署（Linux / systemd / Nginx）

所有 `acp.example.org` 均为占位域名。替换两个 Nginx 模板中的域名与证书路径；在 `/etc/acp-gateway/admin-origin.env`（root:root，0600）中写入 `ACP_ADMIN_ORIGIN=https://你的实际域名`（无尾斜杠）。管理与注册服务使用同一个固定 Origin，不根据请求 Host 自动放行。升级已有实例时，先创建该文件，再安装两个管理服务单元并重载。未配置管理域名时管理服务不会启动，不影响独立的本地推理网关。

部署前按[首页](../README.md)安装依赖并预取词表。不要在应用目录保存真实密钥、令牌、证书私钥或原生调试日志。

Phase 4 目标拓扑：HTTPS Nginx → `acp-router.socket`（按会话和结果 ID 固定槽位，不跨槽重试）→ `/run/acp-gateway-sockets/labN.sock` → 每个账号独立系统用户、HOME 与 ACP 会话。旧的端口示例 `nginx.conf.example` 不用于本站。HTTPS /admin/ 使用独立 Basic 管理员身份，不接受网关密钥。后台要求 Python 3.11+、aiohttp，支持 64 个独立槽位。上线顺序：先创建无登录系统用户 acp-registry；将 registry.py、quota.py、admin.py、admin.html、ui.js 安装至 /opt/acp-gateway/deploy（root 所有，不允许服务用户写）；将 acp-add-slot 以 root:root 0755 安装至 /usr/local/sbin/acp-add-slot；用 visudo 校验并安装模式 0440 的 /etc/sudoers.d/acp-registry，其内容仅为 `acp-registry ALL=(root) NOPASSWD: /usr/local/sbin/acp-add-slot ""`。安装 acp-registry.{socket,service} 至 /etc/systemd/system，daemon-reload 后 enable --now acp-registry.socket；acp-admin@.{service,socket} 安装至 /etc/systemd/system，daemon-reload 后 enable --now acp-admin@lab{1,2}.socket；配置 /etc/nginx/agy-admin.htpasswd（root:www-data 0640）并将 admin-guard.conf 安装至 /etc/nginx/snippets/agy-admin-guard.conf，最后安装 agy-api.conf、nginx -t 后 reload。网页支持动态创建槽位；已绑定账号的用量来自 Google 返回的配额分组或模型，显示剩余比例及可用的重置时间。查询失败不视为额度耗尽；实际路由以池探测为准。只导入本人或获授权管理的个人账号凭据。

## 授权

在可信 SSH 终端交互操作，每个实例使用不同的授权账号：

```sh
sudo -u acp-lab1 -H /opt/acp-gateway/bin/agy acp --login
sudo -u acp-lab2 -H /opt/acp-gateway/bin/agy acp --login
/usr/local/sbin/acp-pool-sync
```

在服务器 `/etc/acp-gateway/shared.env`（root:root，0600）配置 `GATEWAY_API_KEY`，仅供运维探测。团队另设 `GATEWAY_API_KEYS_JSON`，值为 `{"成员标识":"该成员独立的随机密钥"}`（每人至少 24 个随机 ASCII 字符；建议使用 `openssl rand -hex 32`）。不要向所有成员分发运维共享密钥。更新后在维护窗口重启 worker。不要复制 OAuth 验证码或账号 token 到聊天或公共文件。

## 账号池

Phase 4 安装（维护窗口；先排空旧会话，升级会中断旧进程）：
1. 创建无登录用户 `useradd --system --no-create-home --shell /usr/sbin/nologin acp-router`（若不存在）。槽位 HOME 仅账号可读，目录 /run/acp-gateway-sockets 必须 root 所有，不允许 www-data 写入。
2. 安装当前网关 Python 文件和依赖；安装 root 所有的 deploy/acp_router.py 到 /opt/acp-gateway/deploy/，安装 acp-router.{service,socket}、acp-gateway@.{service,socket}、acp-pool-sync.{service,timer} 到 /etc/systemd/system/，安装可执行文件 /usr/local/sbin/acp-pool-sync。运行 `systemctl daemon-reload` 和 `systemctl enable --now acp-pool-sync.timer`，重启已配置的 worker 服务以加载 ACP_SLOT，启用 `systemctl enable --now acp-router.socket`。
3. 运行 `systemctl start acp-pool-sync.service`，确认 pool.json 列出已认证槽位；通过 router.sock 用授权 key 验证 /v1/models。验证后备份旧 vhost，安装 agy-api.conf，`nginx -t && systemctl reload nginx`。回滚路由可恢复旧 vhost 再测试和 reload；不会迁移进行中的工具调用。
4. 隐式亲和缓存最多 4096 项、滑动 30 分钟；它只决定转发位置，不替代 worker 的租户校验。带槽位的工具或响应 ID 所属槽位下线时直接报错。无亲和的新请求优先选择在途请求最少的槽位，同负载轮转。

`acp-pool-sync.timer` 约每 5 分钟扫描 `/etc/acp-gateway/labN.env`，经 systemd 管理的 Unix socket 验证 `/v1/models`。新账号探测成功才加入；未登录、socket 不存在或连接被拒会摘除。繁忙、超时或部分临时错误保留原状态，不对同一推理请求跨账号重试。模型发现成功不保证所有模型都有额度，分配前应逐账号进行流式验证。空池统一返回 503；同步器原子发布 `/run/acp-gateway-sockets/pool.json`；路由器在下一个请求读取新版本，无需 reload。保留旧版 Nginx include 以支持迁移。手动状态：`systemctl status acp-pool-sync.timer`、`journalctl -u acp-pool-sync.service`、`/etc/nginx/acp-gateway/members.inc`。

## 新增第三个账号示例（服务器 root）

```sh
useradd --system --user-group --create-home --home-dir /var/lib/acp-lab3 --shell /usr/sbin/nologin acp-lab3
chmod 700 /var/lib/acp-lab3
install -m600 /dev/null /etc/acp-gateway/lab3.env
printf 'AGY_BIN=/opt/acp-gateway/bin/agy\n' > /etc/acp-gateway/lab3.env
systemctl enable --now acp-gateway@lab3.socket
sudo -u acp-lab3 -H /opt/acp-gateway/bin/agy acp --login
/usr/local/sbin/acp-pool-sync
```

下线先将 `labN.env` 改名为非 `.env` 后缀，运行同步，待流式请求结束，再停止并禁用相应 socket 和 service。域名证书由 Certbot webroot `/var/www/acp-acme` 自动续期，HTTP 仅提供 ACME challenge 和 HTTPS 跳转。
