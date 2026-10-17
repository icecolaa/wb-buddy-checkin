# WorkBuddy 加油站签到

一条命令办完 WorkBuddy「Buddy 加油站」的每日积分：**签到 → 连签进度 → 猫猫旅行**。
全部幂等，重复执行不会重复领取。

- 纯 Python 标准库，**零第三方依赖**
- **不接触任何凭据**：鉴权由 WorkBuddy 桌面端自己完成
- 支持 Windows（命名管道）/ macOS / Linux（unix socket）

## 为什么要单独做这个

每天 100 积分，断一天连签清零，前面几天全白干。手动点很烦，交给定时任务最合适。

本项目的取舍是：**宁可功能少一点，也不碰你的登录态**。
不解析加密信封、不读进程内存、不落盘 token —— 只用客户端自己开的 IPC 通道，
让客户端用自己的身份去发请求。因此客户端怎么改登录态加密格式，这里都不会失效。

代价只有一条：**运行时要开着 WorkBuddy 客户端**（并且已登录）。
需要「客户端不开也能签」的话，得保存凭据，那是另一类方案，本项目刻意不做。

## 安装

放到 WorkBuddy 的技能目录：

```
Windows:  C:\Users\<你>\.workbuddy\skills\wb-buddy-checkin\
macOS:    ~/.workbuddy/skills/wb-buddy-checkin/
```

或直接 `git clone` 到上述位置。

## 用法

```bash
python scripts/checkin.py --self-test     # 离线自检：不联网、不读登录态
python scripts/checkin.py --doctor        # 环境自检（改版后第一件事）
python scripts/checkin.py --check-only    # 只读：今天签没签 / 连签 / 余额 / 旅行状态
python scripts/checkin.py                 # 签到 + 连签 + 旅行闭环
python scripts/checkin.py travel          # 只看旅行（只读）
python scripts/checkin.py travel --travel-auto   # 允许领取 + 派遣
python scripts/checkin.py --json          # 结构化输出
python scripts/checkin.py --no-travel     # 只签到
```

### 定时任务

在 WorkBuddy 里建一条自动化，每天 09:00 执行：

```
python <技能目录>/scripts/checkin.py --json
```

提示词模板见 [`references/examples.md`](references/examples.md)。

电脑不常全天开机的话，可以设多时点（9 / 12 / 15 / 18 / 21 点各一次）补签，
脚本幂等，重复执行不会重复领取；也可以先跑 `--check-only` 再决定要不要跑全流程。

## 工作原理

```
本脚本  --(NDJSON over 命名管道)-->  WorkBuddy 桌面端  --(带登录态)--> 云端接口
```

1. 从 `~/.workbuddy/wbipc/endpoint.json` 读出 `endpoint` 与 `ticket`
2. NDJSON 一帧一行传输，四帧 HMAC-SHA256 挑战应答完成双向身份验证：

   ```
   C -> session_hello     {protocol_min, protocol_max, client_nonce, ticket_id, client}
   S -> session_challenge {server_nonce, server_proof}
   C -> session_prove     {client_proof}
   S -> session_hello_ack {connection_epoch, pipes}
   ```

3. 在同一通道上跑 JSON-RPC 2.0：先 `broker/GetPipe` 申请 `wb.request` 通道，
   再用 `http.fetch` 让客户端代发 HTTP 请求（`path` 必须是相对路径）

脚本发出去的只有相对路径和 JSON body，**没有鉴权头**——那部分由客户端自己附加。
协议细节与踩过的坑记在 [`references/protocol.md`](references/protocol.md)。

## 命令与开关

| 开关 | 作用 |
|---|---|
| `--self-test` | 离线自检协议实现（不联网、不读登录态） |
| `--doctor` | 环境自检：通道、握手、活动状态、Buddy 与旅行状态 |
| `--check-only` / `--status` / `--probe` | 只读探测，一个写请求都不发 |
| `travel` | 只跑旅行；默认只读，加 `--travel-auto` 才领取并派遣 |
| `--location 1-4` | 指定派遣地点，缺省随机（四地收益相同） |
| `--json` | 结构化输出 |
| `--no-travel` | 跳过猫猫旅行 |
| `--no-depart` | 只领取已到达的奖励，不发起新派遣 |

**不提供**档位兑换与连登抽奖：一个是消费行为，一个带随机性，默认替你做不合适。

## 结果契约

`--json` 输出固定结构，三条判断即可：

1. `ok` 是否为 `true`
2. `steps[].status` 是否都落在合法状态集内
3. `anomalies` 是否为 `[]`

| 步骤 | 合法状态 |
|---|---|
| `checkin` | `success` / `already_checked` / `pending` / `skipped` / `error` |
| `streak` | `ready` / `locked` / `skipped` / `error` |
| `travel` | `departed` / `claimed` / `traveling` / `arrived` / `idle` / `idle_limit` / `no_buddy` / `skipped` / `error` |

`traveling` / `arrived` / `idle_limit` / `no_buddy` / `pending` 都是正常终态，不是失败；
`error` 是合法状态，但一定同时出现在 `anomalies` 里。

## 退出码

`0` 成功（含幂等跳过）｜`1` 接口 / 网络 / 业务错误｜`2` 参数错误｜`3` IPC 通道不可用

## 排错

| 现象 | 处理 |
|---|---|
| 退出码 3 | 先跑 `--doctor`；通道要求 WorkBuddy 桌面端正在运行且已登录 |
| `E_PROTOCOL_MISMATCH` | 客户端改了 IPC 协议，需要更新本 Skill |
| `E_AUTH` / `auth_failed` | 客户端重启导致票据轮换，脚本会自动重试一次，仍失败就重开客户端 |
| `E_TIMEOUT` | 客户端无响应，重启客户端 |
| `E_BAD_REQUEST: path must be relative` | 传了绝对 URL，客户端只接受相对路径 |
| 旅行一直 `idle_limit` | 今日派遣次数用完，明天再来 |
| 旅行 `no_buddy` | 确实没有 Buddy（已查过 `/activity/growth/buddy/info`）：客户端「成长计划」→ 领取一只 → 手动点「领取奖励」 |
| `travel.buddy_id` 为 0 但 status 是 `idle_limit` | **不是异常**：`travel/status` 在没有进行中的旅行时会把 `buddy_id` 归零，实际 Buddy 以 `buddy/info` 为准 |
| 余额看着没变 | 接口返回的余额可能是领取前的快照，以加油站面板为准 |

## 目录结构

```
.
├── SKILL.md                    # 技能定义（含 SkillHub frontmatter）
├── README.md
├── CHANGELOG.md
├── LICENSE
├── references/
│   ├── protocol.md             # IPC 协议备忘（握手 / RPC / 已知坑）
│   └── examples.md             # 自动化提示词与输出样例
└── scripts/
    ├── wbipc.py                # IPC 客户端：发现、握手、JSON-RPC、HTTP 转发
    └── checkin.py              # 业务层：签到 / 连签 / 旅行 / 自检
```

## 安全说明

- 只读 `endpoint.json` 里的 `endpoint` 与 `ticket`，两者都只用于本机握手
- 不解析加密登录态，不读进程内存，不写任何凭据到磁盘
- 日志里没有 token —— 脚本从头到尾没拿到过它
- 无 Electron、无常驻进程、无系统级安装、不注册开机项

## License

MIT，见 [LICENSE](LICENSE)。
