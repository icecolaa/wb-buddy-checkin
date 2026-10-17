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

调度：`FREQ=DAILY;BYHOUR=9;BYMINUTE=0`，`validUntil` 用活动截止日。

```
执行 WorkBuddy「Buddy 加油站」每日自动签到，完成签到 / 连签查看 / 猫猫旅行（全部幂等）。

命令：
python "<技能目录>/scripts/checkin.py" --json

判断规则（按结果契约，不要读源码、不要展开推理）：
1. ok 是否为 true
2. steps[].status 是否都落在合法状态集内
3. anomalies 是否为 []

合法状态集：checkin = success/already_checked/pending/skipped/error；
streak = ready/locked/skipped/error；
travel = departed/claimed/traveling/arrived/idle/idle_limit/no_buddy/skipped/error。
其中 traveling（猫在途）、idle_limit（今日次数已用完）、arrived（已到达待领）、
no_buddy（账号没领 Buddy）都是正常终态，不是失败。

退出码：0 = 成功（含幂等跳过）；1 = 接口/业务错误；2 = 参数错误；
3 = 通道不可用 —— 先跑 --doctor，通道要求 WorkBuddy 桌面端正在运行且已登录。

不要加 --redeem / --lottery 之类的动作（本 Skill 不提供）。失败时最多重试一次，
仍失败就用一句话说明原因，不要反复重试。

汇报：一句话给出签到结果（签到成功 / 今日已签）+ 余额 + 连签天数 +
猫猫旅行状态（已派遣 / 在途几分钟 / 已领奖）。如果 streak 显示距下一档还差几天，顺带提一句。
```

不确定当天跑过没有时，把第一条命令换成：

```
python "<技能目录>/scripts/checkin.py" --check-only --json
```

看到 `today_checked_in: true` 就只汇报、不再跑全流程。

## 3. Agent 处置流程

| 情况 | 动作 |
|---|---|
| 退出码 0 | 按上面模板汇报一句话即可 |
| 退出码 1 | 读 `anomalies`，只报实际失败的那一步，别的步骤结果照常汇报 |
| 退出码 3 | 先跑 `--doctor`：能通说明票据轮换，重跑一次；还是 3 就提示用户开客户端 |
| `travel = no_buddy` | 提示用户去客户端「成长计划」领一只 Buddy，不要重试 |
| `travel = idle_limit` | 说明今日次数已用完，明天自动恢复 |
| `checkin = already_checked` | 正常，不要重试、不要换命令再试一次 |

## 4. 输出样例

```json
{
  "schema": 1,
  "ok": true,
  "version": "1.1.0",
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
