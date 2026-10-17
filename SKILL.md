---
slug: wb-checkin-icecola
displayName: WorkBuddy 加油站每日签到
display_name: WorkBuddy 加油站每日签到
name: wb-checkin-icecola
version: 1.0.0
summary: 一条命令搞定 WorkBuddy「Buddy 加油站」每日签到、连签进度与猫猫旅行。走客户端本机 IPC，零第三方依赖，全程不接触任何凭据。
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
description: WorkBuddy「Buddy 加油站」每日签到自动化。当用户说「每天自动签到」「帮我领 Buddy 加油站积分」「现在签个到」「查一下连签天数」「派猫猫旅行」时使用。纯 Python 标准库实现，经客户端本机 IPC 转发请求，鉴权由 WorkBuddy 桌面端自己完成，脚本不读取、不保存、不打印任何 token。
description_zh: WorkBuddy 加油站每日签到与猫猫旅行，纯标准库，零凭据接触。
description_en: Daily check-in and buddy travel for WorkBuddy, stdlib only, no credential access.
author: 冰可乐
---

# WorkBuddy 加油站每日签到

一条命令办完每日积分：**签到 → 连签进度 → 猫猫旅行**。全部幂等，重复跑不会重复领取。

纯 Python 标准库，不装任何第三方包，不需要配置 token。

## 快速开始

```bash
python scripts/checkin.py --doctor   # 第一次先自检（改版后也先跑这个）
python scripts/checkin.py --probe    # 只读探测，绝不发起写请求
python scripts/checkin.py            # 正常执行：签到 + 连签 + 旅行
python scripts/checkin.py --json     # 结构化输出，给 Agent 用
```

## 它是怎么工作的

WorkBuddy 桌面端运行时会在本机开一个 IPC 服务，地址和票据写在
`~/.workbuddy/wbipc/endpoint.json`。本 Skill 连上去，做一次
**HMAC-SHA256 挑战应答**握手（双向验证），然后在同一条通道上跑 JSON-RPC，
把 HTTP 请求交给客户端代发：

```
本脚本  --(NDJSON over 命名管道)-->  WorkBuddy 桌面端  --(带登录态)--> 云端接口
```

关键点：**鉴权头由客户端按它自己的登录态附加**。脚本只发相对路径和 JSON body，
既拿不到也不需要 access token，因此不受登录态加密格式改版影响，日志里也不可能有凭据。

握手与帧格式的实现细节见 `scripts/wbipc.py` 顶部注释。

## 命令与开关

| 开关 | 作用 |
|---|---|
| `--doctor` | 环境自检：通道、握手、活动状态、Buddy 与旅行状态 |
| `--probe` | 只读探测，不发任何写请求 |
| `--json` | 结构化输出 |
| `--no-travel` | 跳过猫猫旅行 |
| `--no-depart` | 只领取已到达的奖励，不发起新派遣 |

本 Skill **不提供**档位兑换与连登抽奖：前者是消费行为，后者带随机性，替你默认做不合适。

## 结果契约（给 Agent 用）

`--json` 输出结构固定，三条判断即可，不要读源码：

1. `ok` 是否为 `true`
2. `steps[].status` 是否都落在合法状态集内
3. `anomalies` 是否为 `[]`

| 步骤 | 合法状态 |
|---|---|
| `checkin` | `success` / `already_checked` / `skipped` |
| `streak` | `ready` / `locked` / `skipped` |
| `travel` | `departed` / `claimed` / `traveling` / `idle_limit` / `no_buddy` / `skipped` |

`traveling`（猫在途）与 `idle_limit`（今日派遣次数已用完）是**正常终态**，不是失败。
`no_buddy` 表示账号还没领取 Buddy，需要先去客户端的「成长计划」领一只。

## 退出码

`0` 成功（含幂等跳过）｜`1` 接口 / 网络错误｜`3` IPC 通道不可用

## 排错

| 现象 | 处理 |
|---|---|
| 退出码 3 | 先跑 `--doctor`。通道要求 WorkBuddy 桌面端**正在运行且已登录** |
| `E_PROTOCOL_MISMATCH` | 客户端改了 IPC 协议，本 Skill 需要更新 |
| `E_AUTH` / `auth_failed` | 握手校验失败，通常是客户端重启导致票据轮换，重跑即可 |
| `E_TIMEOUT` | 客户端无响应；关掉重开一次客户端 |
| 领取成功但余额没变 | 接口返回的余额可能是领取前的快照，以加油站面板为准 |
| 旅行一直 `idle_limit` | 今日派遣次数用完，明天再来 |

## 边界（不做的事）

- 不读取、不入库、不打印任何 token —— 脚本根本没有拿到它的途径
- 不修改 WorkBuddy 任何本体文件，不装常驻进程，不注册开机项
- 只做领取类操作，不删除服务端任何数据
- 不替用户消费（无兑换）、不替用户抽奖

## 日志

默认写入 `~/.workbuddy/wb-buddy-checkin/checkin.log`，单行结构化，可用
`WB_CHECKIN_LOG` 覆盖路径。

MIT License，见 `LICENSE`。
