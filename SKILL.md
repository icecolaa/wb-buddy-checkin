---
slug: wb-checkin-icecola
displayName: WorkBuddy 加油站签到
display_name: WorkBuddy 加油站签到
display_name_en: WorkBuddy Buddy-Station Check-in
name: wb-checkin-icecola
version: 1.1.0
summary: 一条命令搞定 WorkBuddy「Buddy 加油站」每日签到、连签进度与猫猫旅行。走客户端本机 IPC 代发请求，零第三方依赖，全程不接触任何凭据。
license: MIT
homepage: https://github.com/
category: 自动化
tags:
  - 签到
  - 自动化
  - Buddy加油站
  - 积分
  - 猫猫旅行
  - 零依赖
  - 隐私
description: >-
  WorkBuddy「Buddy 加油站」每日签到与猫猫旅行自动化，走客户端本机 IPC 代发请求，不读取任何 token。
  当用户说「每天自动签到 / 每日签到 / 自动领 Buddy 加油站积分 / 现在签个到 / 帮我签到一次 / WorkBuddy 打卡 /
  查一下连签天数 / 我的积分还有多少 / 今天签到了吗 / 派猫猫旅行 / 猫猫旅行 / 领旅行奖励 / 自动派猫猫 /
  Buddy 在旅行吗 / 旅行什么时候回来 / 检查签到环境 / 签到脚本报错 / 客户端更新后签不了 / 怎样定时签到 /
  设置每日自动签到」时使用。
  三种模式：默认全流程（签到 + 连签 + 旅行闭环）；--check-only 纯只读；travel 只跑旅行。
  一条通道、零配置：只要 WorkBuddy 桌面端在运行且已登录，无需 token、无需授权、无需网页会话。
description_zh: >-
  WorkBuddy 加油站每日签到 / 连签 / 猫猫旅行，本机 IPC 代发，零凭据接触，纯标准库。
description_en: >-
  Daily check-in, streak tracking and Buddy travel for WorkBuddy through the desktop client's local IPC,
  with zero credential access and no third-party dependencies.
author: 冰可乐
---

# WorkBuddy 加油站签到

一条命令办完每日积分：**签到 → 连签进度 → 猫猫旅行**。全部幂等，重复跑不会重复领取。

纯 Python 标准库，不装任何第三方包，不配置、不读取、不打印任何 token。

> 设计取向：**只走一条通道 —— 客户端本机 IPC**。代价是运行时要开着客户端；换来的是
> 脚本从头到尾碰不到凭据，因此不受登录态加密格式改版影响，也没有「令牌泄漏」这个失败模式。
> 如果你需要「客户端不开也能签」，那是另一类方案（要保存凭据），本 Skill 刻意不做。

## 快速开始

```bash
python scripts/checkin.py --self-test    # 离线自检：不联网、不读登录态（首次可先跑这个）
python scripts/checkin.py --doctor       # 环境自检：通道 / 接口 / Buddy / 旅行状态（只读）
python scripts/checkin.py --check-only   # 只读查询：今天签没签、连签几天、余额、猫在不在路上
python scripts/checkin.py                # 全流程：签到 + 连签 + 旅行闭环
python scripts/checkin.py --json         # 结构化输出，给 Agent / 自动化用
```

## 三种模式

| 模式 | 命令 | 会不会写服务端 |
|---|---|---|
| 全流程（默认） | `checkin.py` | 会：签到、领旅行奖励、派遣（派遣前先查上限） |
| 只读 | `checkin.py --check-only`（同 `--probe` / `--status`） | **不会**，一个写请求都不发 |
| 只跑旅行 | `checkin.py travel` | **不会**；加 `--travel-auto` 才会领取并派遣 |

Agent 在不确定当天状态时，先跑 `--check-only`，再决定要不要跑全流程 —— 这比反复试错省事。

## 它怎么工作

WorkBuddy 桌面端运行时会在本机开一个 IPC 服务，地址与票据写在
`~/.workbuddy/wbipc/endpoint.json`。本 Skill 连上去做一次 **HMAC-SHA256 挑战应答**
握手（双向验证），然后在同一条通道上跑 JSON-RPC，把 HTTP 请求交给客户端代发：

```
本脚本  --(NDJSON over 命名管道 / unix socket)-->  WorkBuddy 桌面端  --(带登录态)--> 云端接口
```

关键点：**鉴权头由客户端按它自己的登录态附加**。脚本只发相对路径和 JSON body，
既拿不到也不需要 access token。因此：

- 客户端 5.6.2+ 的登录态静态加密（AES-GCM 信封）对本 Skill **无影响**；
- 日志、异常、JSON 输出里**不可能出现凭据**；
- 换机器、换账号都不用重新配置，客户端是什么身份就用什么身份。

协议细节（握手四帧、transcript 构造、RPC 调用约束、Windows 命名管道的坑）见
`references/protocol.md`。

## 接口事实（给维护者）

| 用途 | 方法与路径 | 说明 |
|---|---|---|
| 签到状态 | `POST /v2/billing/meter/checkin-activity-status` | `today_checked_in` / `streak_days` / `total_credits` / `end_time` / `active` |
| 领取签到 | `POST /v2/billing/meter/daily-checkin` | 成功 200 `code:0`；**已签到返回 400 + `code:10001`** |
| 旅行状态 | `GET /activity/growth/buddy/travel/status` | `state` = `idle` / `traveling` / `arrived`，另有 `daily_limit_reached` / `arrive_at` / `reward_credit` |
| 领旅行奖励 | `POST /activity/growth/buddy/travel/claim` | 仅 `arrived` 可领 |
| 派遣 | `POST /activity/growth/buddy/travel/depart` | body `{"location_id": 1-4}`；仅 `idle` 且未达上限 |

关于路径的两个反直觉点（实测）：

1. **路径给 `/activity/...` 就够了**，客户端自己会路由到对应域名。不要拼绝对 URL ——
   `http.fetch` 明确拒绝：`path must be relative and start with /`。
2. 前缀 `/v2` 在活动类路径上带不带都能通（客户端会归一化），不必纠结。

## 结果契约（给 Agent 用）

`--json` 输出结构固定，三条判断即可，**不要读源码、不要展开推理**：

1. `ok` 是否为 `true`
2. `steps[].status` 是否都落在合法状态集内
3. `anomalies` 是否为 `[]`

| 步骤 | 合法状态 |
|---|---|
| `checkin` | `success` / `already_checked` / `pending` / `skipped` / `error` |
| `streak` | `ready` / `locked` / `skipped` / `error` |
| `travel` | `departed` / `claimed` / `traveling` / `arrived` / `idle` / `idle_limit` / `no_buddy` / `skipped` / `error` |

这些是**正常终态**，不要当失败：

- `traveling` —— 猫在路上，`minutes_left` 是剩余分钟；
- `idle_limit` —— 今天的派遣次数已用完；
- `arrived` —— 已到达待领取（只读模式才会出现，全流程会自动领）；
- `no_buddy` —— 账号还没领 Buddy，属账号状态，去客户端「成长计划」领一只即可；
- `pending` —— 只读模式下的「今天还没签」。

`error` 是合法状态，但一定同时出现在 `anomalies` 里。

顶层字段：`schema`（结构版本，当前 1）、`ok`、`version`、`mode`、`channel`、`steps`、
`anomalies`、`balance`、`streak_days`、`today_credit`、`travel_reward`、`elapsed`、
`generated_at`（仅 `--json`）。

## 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| `0` | 成功（含幂等跳过） | 无需处理 |
| `1` | 接口 / 网络 / 业务错误 | 看 `anomalies` |
| `2` | 参数错误 | 修命令 |
| `3` | IPC 通道不可用 | 客户端没开 / 没登录 / 协议变了，先跑 `--doctor` |

## 设置每日自动签到

用 WorkBuddy 自带自动化（**不要**用 crontab / 计划任务 / launchd —— 那些拿不到客户端的
IPC 会话，也会在锁屏时失败）。让 Agent 用 `automation_update` 创建：

- `name`：`WorkBuddy 加油站每日签到`
- `rrule`：`FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
- `validUntil`：活动截止日（当前 `2026-10-31`）
- 提示词模板见 `references/examples.md`

提示词里要写清三件事：**命令、结果契约、失败时怎么报**。否则 Agent 每次都要重新读源码。

## 排错

| 现象 | 处理 |
|---|---|
| 退出码 3 | 先跑 `--doctor`。通道要求 WorkBuddy 桌面端**正在运行且已登录** |
| `E_NO_ENDPOINT` | 客户端没启动过，或 `WORKBUDDY_CONFIG_DIR` 指错了 |
| `E_AUTH` | 握手校验失败，通常是客户端重启后票据轮换；脚本会自动重试一次，仍失败就重开客户端 |
| `E_PROTOCOL_MISMATCH` | 客户端改了 IPC 协议，本 Skill 需要更新 |
| `E_TIMEOUT` | 客户端无响应；关掉重开一次客户端 |
| 领取成功但余额没变 | 返回的余额可能是领取前的快照（同一次响应内），以加油站面板为准 |
| 旅行一直 `idle_limit` | 今日派遣次数用完，明天自动恢复 |
| 旅行 `no_buddy` | 账号没领 Buddy：客户端「成长计划」→ 领取一只 Buddy → 手动点「领取奖励」 |
| 猫一直 `traveling` | 官方没有召回接口，只能等到达后自动领；积分不会丢 |
| 只想确认没乱发请求 | 用 `--check-only` 或 `travel`（不带 `--travel-auto`），零写请求 |

## 边界（不做的事）

- 不读取、不入库、不打印任何 token —— 脚本根本没有拿到它的途径
- 不修改 WorkBuddy 任何本体文件，不装常驻进程，不注册开机项
- 不模拟点击客户端 UI（Agent 也点不了）：签到是一次带鉴权的接口请求，不是 GUI 流程
- 只做领取类操作，不删除服务端任何数据
- 不替用户消费（无档位兑换）、不替用户抽奖（无连登抽奖）
- 派遣前必查 `daily_limit_reached`，达上限时一个写请求都不发

## 日志

默认写入 `~/.workbuddy/wb-buddy-checkin/checkin.log`，单行结构化（时间 / 结论 / 各步状态 / 余额）。
用 `WB_CHECKIN_LOG` 覆盖路径。日志里同样不含任何凭据。

MIT License，见 `LICENSE`。
