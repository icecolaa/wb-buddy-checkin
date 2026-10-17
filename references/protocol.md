# wbipc 协议备忘（维护者用）

这里记录的是本机 IPC 的实测行为，用来排错和判断「客户端改版后还能不能跑」。
所有事实都来自客户端自身的实现与本机实测，不含任何凭据。

## 1. 发现

```
~/.workbuddy/wbipc/endpoint.json
{"endpoint": "\\\\.\\pipe\\wbipc-xxxx" | "/tmp/xxx.sock", "ticket": "<32B base64url>"}
```

- 目录可用环境变量 `WORKBUDDY_CONFIG_DIR` 覆盖（脚本已支持）。
- 客户端重启后 **endpoint 与 ticket 都会换**，所以每次运行都要重读文件，
  不能把路径写死进配置。
- `ticket` 是 32 字节随机数的 base64url（43 字符，无填充）。

## 2. 传输

**NDJSON**：一帧一行，UTF-8，行内不允许裸换行。

这是个容易踩的坑：客户端二进制里同时存在 `Content-Length` 字样（那是别的模块），
很容易误判成 LSP 帧。实测只有 NDJSON 能打通。

## 3. 握手（四帧，HMAC-SHA256 双向验证）

```
C -> session_hello      {protocol_min, protocol_max, client_nonce, ticket_id, client}
S -> session_challenge  {server_nonce, server_proof}
C -> session_prove      {client_proof}
S -> session_hello_ack  {connection_epoch, pipes}
```

- `ticket_id = sha256(ticket).hex()[:16]`
- `client_nonce` = 16 字节随机数的 base64url
- `proof = base64url(HMAC-SHA256(key=ticket, msg=transcript))`
- `transcript` = 五段，每段前置 **大端 uint32 长度前缀**：

  | 顺序 | 内容 |
  |---|---|
  | 1 | `b"wbipc-c"`（客户端）/ `b"wbipc-s"`（服务端） |
  | 2 | 协议版本（`b"1"`） |
  | 3 | endpoint 字符串 |
  | 4 | client_nonce |
  | 5 | server_nonce |

任一段变化都会让证明失效；服务端 proof 用 `wbipc-s` 前缀，客户端用 `wbipc-c`，
必须用 `hmac.compare_digest` 反向校验，不要只校验自己这一侧。

`session_hello_error` 的 `code`：`auth_failed` → 票据不对（客户端重启过）；
其余 → 协议不兼容。

## 4. 调用

先向 broker 申请通道，再带 `mode: "call"` 调方法：

```
broker/GetPipe  {pipe: "wb.request"}      ->  {channel: "c:wb.request", methods: [...]}
c:wb.request/http.fetch                   ->  {status, headers, body_b64}
```

- **`mode: "call"` 是必须的**，缺了会一直不返回。
- `http.fetch` 的 `params`：`{method, path, query?, headers?, body_b64?}`
- `path` **必须是相对路径且以 `/` 开头**，绝对 URL 会被直接拒绝：
  `path must be relative and start with /`。域名由客户端按路径自己路由。
- `body_b64` 是请求体的 base64；GET 不传。
- 响应体也是 base64，需要自己解。
- 可见管道会随版本变化（实测出现过 `wb.request` / `wb.entitlement` / `wb.wecom-a2a`），
  不要硬编码管道清单，用 ack 里的 `pipes` 判断。

## 5. Windows 命名管道的坑

同一个管道句柄上「一个线程阻塞读 + 另一个线程写」会直接 `EINVAL`。

正确做法：任意时刻只做一种操作 —— 写完整帧，然后临时起一个读线程读一帧并 join 超时。
`scripts/wbipc.py` 已经按这个模型实现，加并发逻辑前先想清楚这一点。

## 6. 已知错误码

| code | 含义 | 处理 |
|---|---|---|
| `E_NOT_CONNECTED` | 管道连不上 / 中途断开 | 客户端没开，或重启了（重读 endpoint 重试） |
| `E_AUTH` | 握手 proof 校验失败 | 票据轮换，重连即可 |
| `E_NO_ENDPOINT` | 找不到 endpoint.json | 客户端从未启动 |
| `E_PROTOCOL_MISMATCH` | 帧格式 / 方法名变了 | 需要更新本 Skill |
| `E_TIMEOUT` | 等不到响应 | 客户端卡住，重开 |
| `E_UPSTREAM` | 上游 HTTP 非 2xx 且响应体不是 JSON | 看报错里的状态码与截断 body |
| `E_BAD_REQUEST` | 参数不合规（如绝对 URL） | 改成本地相对路径 |
| `E_BIZ_<code>` | 业务返回码非 0 | 由业务层按语义处理 |

## 7. 接口行为备忘（踩过的坑）

- **`travel/status` 的 `buddy_id` 不可信**：只有在「有进行中的旅行」时才返回真实 ID，
  其余状态（idle / idle_limit / 已领完）一律返回 `0`。判断账号到底有没有 Buddy
  必须查 `GET /activity/growth/buddy/info`（返回 `data.buddy.instance_id` / `name` /
  `rarity` / `personality`）。1.1.0 曾据此把「猫在家」误报成 `no_buddy`。
- **`http.fetch` 的 `path` 必须相对**：传绝对 URL 会被拒，`E_BAD_REQUEST: path must be relative and start with /`。
- **路径前缀会被归一化**：`/v2/activity/...` 与 `/activity/...` 都能通，不需要纠结。
- **签到接口用 HTTP 400 表示「今天已签」**：body 里 `code: 10001` 才是真正的信息，别只看状态码。

## 8. 手工探针

判断「是脚本坏了还是通道坏了」时，最短的自检：

```bash
python scripts/wbipc.py            # 连接 + 握手 + 打印 epoch 与可见管道
python scripts/checkin.py --doctor # 再加上接口与 Buddy 状态
```

需要看原始返回时，用 `scripts/wbipc.py` 里的 `WbipcClient` 直接发一次请求即可，
注意 `check_status=False` 才能读到 400 的响应体。
