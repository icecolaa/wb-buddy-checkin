# 用法示例

## 1. 日常命令

```bash
# 全流程：签到 → 连签 → 旅行（先领后派）
python scripts/checkin.py

# 只看状态，零写请求
python scripts/checkin.py --check-only
python scripts/checkin.py --check-only --json

# 只跑旅行（只读）
python scripts/checkin.py travel
# 只跑旅行，允许领取 + 派遣
python scripts/checkin.py travel --travel-auto

# 指定派遣地点（1-4，缺省随机）
python scripts/checkin.py --location 3

# 只签到，不碰旅行
python scripts/checkin.py --no-travel

# 只领已到达的奖励，不发起新派遣
python scripts/checkin.py --no-depart

# 排错
python scripts/checkin.py --self-test   # 离线，不联网
python scripts/checkin.py --doctor      # 通道 + 接口 + Buddy
```

## 2. 自动化提示词（直接复制给 `automation_update`）

### 2.1 每天固定一次（推荐）

调度：`FREQ=DAILY;BYHOUR=9;BYMINUTE=0`，`validUntil` 用活动截止日。

```
执行 WorkBuddy「Buddy 加油站」每日签到（本机 IPC 版）。

命令：
python "<技能目录>/scripts/checkin.py" --json

判断规则（按结果契约，不要读源码、不要展开推理）：
1. ok 是否为 true
2. steps[].status 是否都落在合法状态集内
3. anomalies 是否为 []

合法状态集：checkin = success/already_checked/pending/skipped/error；
streak = ready/locked/skipped/error；
travel = departed/claimed/traveling/arrived/idle/idle_limit/no_buddy/skipped/error。
traveling（在途）、idle_limit（今日次数用完）、arrived（待领取）都是正常终态。
error 属于合法状态但必进 anomalies。

退出码：0 = 成功（含幂等跳过）；1 = 接口/业务错误；2 = 参数错误；3 = 通道不可用。
退出码 3 时先跑 --doctor：若只是票据轮换，脚本已自动重连一次，重跑即可；
仍然 3 就一句话说明「WorkBuddy 桌面端未运行或未登录」，结束，不要反复重试。

约束：本技能只走本机 IPC 通道（零凭据），没有 --redeem / --lottery 之类的参数，
也不要自行构造 HTTP 请求或读取任何凭据文件。失败最多重试一次。

汇报：一句话给出签到结果（签到成功 / 今日已签）+ 余额 + 连签天数 +
猫猫旅行状态（已派遣 / 在途几分钟 / 已领奖）。若 streak 显示距下一档还差几天，顺带提一句。
```

### 2.2 高频执行 + 每日静音闸门

调度改成 `FREQ=HOURLY;INTERVAL=2`（应对当时电脑没开的情况）时，脚本幂等不会重复领，
但会每两小时汇报一次。加一层**状态闸门**让它安静下来，同时不漏掉猫猫到点的奖励：

```
执行 WorkBuddy「Buddy 加油站」每日签到（本机 IPC 版）。

第一步，只读探测（零写请求）：
python "<技能目录>/scripts/checkin.py" --check-only --json

根据结果决定要不要往下走：
- travel.state == "arrived"   → 猫已到家待领奖，进入第二步
- checkin.status == "pending" → 今天还没签，进入第二步
- 其余情况（今天已签、猫在途、今日次数已用完）→ 直接结束，不输出任何内容，不要汇报

注意：travel.buddy_id 为 0 且 status 为 idle / idle_limit 是「猫在家歇着」的正常态，
不代表账号没领 Buddy，不要提示用户去领猫。只有 status 明确是 no_buddy 才需要提示。

第二步，全流程（幂等）：
python "<技能目录>/scripts/checkin.py" --json

判断规则与汇报格式同 2.1。不要加 --location，让目的地随机。
```

闸门依据是**真实状态**而不是日期标记文件，所以「猫到点了要领奖」这种情况依然会被触发。

## 3. Agent 处置流程

| 情况 | 动作 |
|---|---|
| 退出码 0 | 按上面模板汇报一句话即可 |
| 退出码 1 | 读 `anomalies`，只报实际失败的那一步，别的步骤结果照常汇报 |
| 退出码 3 | 先跑 `--doctor`：能通说明票据轮换，重跑一次；还是 3 就提示用户开客户端 |
| `travel = no_buddy` | 确实没有 Buddy（已查过 `buddy/info`），提示去客户端「成长计划」领一只，不要重试 |
| `travel = idle_limit` | 今日派遣次数已用完，明天自动恢复。这是每天跑完的常态，不要提示去领猫 |
| `travel.buddy_id = 0` 但 status 不是 no_buddy | 正常：`travel/status` 无进行中旅行时会把 ID 归零，不代表没猫 |
| `checkin = already_checked` | 正常，不要重试、不要换命令再试一次 |

## 4. 输出样例

```json
{
  "schema": 1,
  "ok": true,
  "version": "1.1.1",
  "mode": "full",
  "channel": "wbipc",
  "steps": [
    {"name": "checkin", "status": "already_checked",
     "detail": "活动状态显示今天已签到，未发起领取请求", "credit": 100},
    {"name": "streak", "status": "ready", "days": 2, "today_checked_in": true,
     "week_checkin_days": 2, "active": true, "balance": 200,
     "end_time": "2026-10-31 23:59:59", "today_credit": 100},
    {"name": "travel", "status": "traveling", "state": "traveling",
     "buddy_id": 18632741, "location": "咖啡馆", "reward_credit": 10,
     "daily_limit_reached": true, "arrive_at": "10-17 14:51", "minutes_left": 62}
  ],
  "anomalies": [],
  "balance": 200,
  "streak_days": 2,
  "today_credit": 100,
  "travel_reward": 10,
  "elapsed": 0.22,
  "generated_at": "2026-10-17 13:49:24"
}
```

人类可读输出：

```
WorkBuddy 加油站 2026-10-17 13:49:24

  ☑️ 每日签到：今天已签
  ℹ️ 连签进度：2 天
  🚄 猫猫旅行：在途，10-17 14:51 回来（约 62 分钟）

当前加油站余额：200
```
