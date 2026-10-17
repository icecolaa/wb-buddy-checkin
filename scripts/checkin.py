"""WorkBuddy「Buddy 加油站」每日签到。

走客户端本机 IPC（wb.request 管道），由 WorkBuddy 桌面端用自己的登录态代发
HTTP 请求 —— 本脚本不读取、不保存、不打印任何凭据。

三个步骤全部幂等，重复执行不会重复领取。

退出码
    0  成功（含「今天已签到」这类幂等跳过）
    1  接口 / 网络错误
    3  IPC 通道不可用（客户端没开、未登录，或协议版本不匹配）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wbipc import WbipcClient, WbipcError, config_dir  # noqa: E402

VERSION = "1.0.0"

PATH_ACTIVITY = "/v2/billing/meter/checkin-activity-status"
PATH_CHECKIN = "/v2/billing/meter/daily-checkin"
PATH_TRAVEL = "/activity/growth/buddy/travel"

# 签到接口的业务返回码。10001 是「今天已签到」的实测值；
# 1001/1002/1003 是客户端映射表里的同类语义，一并兼容。
BIZ_ALREADY = {1001, 10001}
BIZ_NOT_ELIGIBLE = 1002
BIZ_ENDED = 1003

LEGAL = {
    "checkin": {"success", "already_checked", "skipped"},
    "streak": {"ready", "locked", "skipped"},
    "travel": {"departed", "claimed", "traveling", "idle_limit", "no_buddy", "skipped"},
}


# ---------------------------------------------------------------- 工具

def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_dir() -> str:
    return os.path.join(config_dir(), "wb-buddy-checkin")


def write_log(line: str) -> None:
    """单行结构化日志。绝不写入 token —— 本脚本本来也拿不到。"""
    path = os.environ.get("WB_CHECKIN_LOG") or os.path.join(log_dir(), "checkin.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def unwrap(resp: dict) -> dict:
    """取出业务数据；code != 0 时抛错（调用方按需要自己处理）。"""
    code = resp.get("code")
    if code != 0:
        raise WbipcError("E_BIZ_%s" % code, resp.get("msg") or "业务返回码 %s" % code, resp)
    return resp.get("data") or {}


def ts_text(value) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


# ---------------------------------------------------------------- 业务

def read_activity(cli: WbipcClient) -> dict:
    return unwrap(cli.http("POST", PATH_ACTIVITY, {}))


def step_checkin(cli: WbipcClient) -> dict:
    out = {"name": "checkin", "status": "skipped"}
    try:
        # check_status=False：签到被拒时接口返回 400 + 业务码，要读 body 才知道原因
        resp = cli.http("POST", PATH_CHECKIN, {}, check_status=False)
        code = resp.get("code")
        message = str(resp.get("msg") or "")
        if code in BIZ_ALREADY or "已签到" in message:
            out["status"] = "already_checked"
            out["detail"] = message or "今天已经签过了"
            return out
        if code == BIZ_NOT_ELIGIBLE:
            out["status"] = "error"
            out["detail"] = message or "账号不具备签到资格"
            return out
        if code == BIZ_ENDED:
            out["status"] = "skipped"
            out["detail"] = message or "活动已结束"
            return out
        if code != 0:
            raise WbipcError("E_BIZ_%s" % code, message or "业务返回码 %s" % code, resp)
        data = resp.get("data") or {}
    except WbipcError:
        raise
    out["status"] = "success"
    for key in ("today_credit", "daily_credit", "total_credits"):
        if key in data:
            out[key] = data[key]
    return out


def step_streak(cli: WbipcClient) -> dict:
    data = read_activity(cli)
    streak = data.get("streak_days") or 0
    out = {
        "name": "streak",
        "status": "ready",
        "days": streak,
        "today_checked_in": bool(data.get("today_checked_in")),
        "bonus_days": data.get("streak_bonus_days") or 0,
        "bonus_credit": data.get("streak_bonus_credit") or 0,
        "active": bool(data.get("active")),
        "balance": data.get("total_credits"),
        "end_time": data.get("end_time"),
    }
    nxt = data.get("next_streak_day") or 0
    if nxt:
        out["next_tier"] = "%dd" % nxt
        out["remaining"] = max(0, nxt - streak)
    if not out["active"]:
        out["status"] = "locked"
        out["detail"] = "活动未开启或已结束"
    return out


def step_travel(cli: WbipcClient, do_depart: bool = True) -> dict:
    data = unwrap(cli.http("GET", PATH_TRAVEL + "/status"))
    state = data.get("state")
    buddy_id = data.get("buddy_id") or 0
    out = {
        "name": "travel",
        "status": "skipped",
        "state": state,
        "buddy_id": buddy_id,
        "location": (data.get("location") or {}).get("name"),
        "reward_credit": data.get("reward_credit"),
        "daily_limit_reached": bool(data.get("daily_limit_reached")),
    }

    if not buddy_id:
        out["status"] = "no_buddy"
        out["detail"] = "账号下还没有 Buddy，先去「成长计划」领取一只"
        return out

    if state == "arrived":
        resp = cli.http("POST", PATH_TRAVEL + "/claim", {})
        if resp.get("code") == 0:
            out["status"] = "claimed"
            out["reward_credit"] = (resp.get("data") or {}).get("reward_credit") \
                or out.get("reward_credit")
        else:
            out["status"] = "error"
            out["detail"] = resp.get("msg") or "领取失败"
        return out

    if state == "traveling":
        out["status"] = "traveling"
        out["arrive_at"] = ts_text(data.get("arrive_at"))
        return out

    if state == "idle":
        if not do_depart:
            out["detail"] = "已按参数跳过派遣"
            return out
        if data.get("daily_limit_reached"):
            out["status"] = "idle_limit"
            out["detail"] = "今天的派遣次数已用完"
            return out
        resp = cli.http("POST", PATH_TRAVEL + "/depart", {"location_id": 1})
        if resp.get("code") == 0:
            out["status"] = "departed"
            out["arrive_at"] = ts_text((resp.get("data") or {}).get("arrive_at"))
        else:
            out["status"] = "error"
            out["detail"] = resp.get("msg") or "派遣失败"
        return out

    out["detail"] = "未知状态：%s" % state
    return out


# ---------------------------------------------------------------- 自检

def doctor() -> int:
    print("wb-buddy-checkin %s ｜ 自检 %s\n" % (VERSION, now_text()))

    from wbipc import discovery_path
    path = discovery_path()
    print("  发现文件：%s" % path)
    print("  %s 存在" % ("✅" if os.path.exists(path) else "❌"))

    try:
        cli = WbipcClient()
        cli.connect()
    except WbipcError as exc:
        print("  ❌ IPC 通道不可用：%s（%s）" % (exc.code, exc))
        print("\n结论：❌ 请确认 WorkBuddy 桌面端正在运行且已登录")
        return 3

    try:
        print("  ✅ 握手成功｜epoch=%s" % cli.epoch)
        print("     可见管道：%s" % ", ".join(cli.pipes))

        try:
            data = read_activity(cli)
        except WbipcError as exc:
            print("  ❌ 活动状态接口：%s" % exc)
            return 1
        print("  ✅ 活动状态：今天%s｜连签 %s 天｜余额 %s"
              % ("已签" if data.get("today_checked_in") else "未签",
                 data.get("streak_days") or 0, data.get("total_credits")))
        if data.get("end_time"):
            print("     活动截止：%s" % data.get("end_time"))
        if not data.get("active"):
            print("     ⚠️ 活动当前 active=false，签到会被跳过")

        try:
            travel = unwrap(cli.http("GET", PATH_TRAVEL + "/status"))
        except WbipcError as exc:
            print("  ⚠️ 旅行接口：%s" % exc)
        else:
            buddy = travel.get("buddy_id") or 0
            print("  %s Buddy：%s｜旅行状态：%s"
                  % ("✅" if buddy else "⚠️", buddy or "未领取", travel.get("state")))
    finally:
        cli.close()

    print("\n结论：✅ 环境正常")
    return 0


# ---------------------------------------------------------------- 主流程

def run(args) -> int:
    if args.doctor:
        return doctor()

    started = dt.datetime.now()
    try:
        cli = WbipcClient(timeout=25.0)
        cli.connect()
    except WbipcError as exc:
        payload = {"ok": False, "version": VERSION, "channel": "wbipc",
                   "error": "%s: %s" % (exc.code, exc), "steps": [], "anomalies": []}
        emit(args, payload)
        write_log("%s | status=no_channel | code=%s" % (now_text(), exc.code))
        return 3

    steps: list[dict] = []
    anomalies: list[str] = []
    balance = None
    try:
        if args.probe:
            try:
                data = read_activity(cli)
                payload = {
                    "ok": True, "version": VERSION, "channel": "wbipc",
                    "probe": True,
                    "active": bool(data.get("active")),
                    "today_checked_in": bool(data.get("today_checked_in")),
                    "streak_days": data.get("streak_days"),
                    "balance": data.get("total_credits"),
                    "end_time": data.get("end_time"),
                }
                emit(args, payload)
                write_log("%s | status=ok | action=probe | balance=%s"
                          % (now_text(), payload["balance"]))
                return 0
            except WbipcError as exc:
                emit(args, {"ok": False, "probe": True, "error": str(exc)})
                return 1

        for name, fn in (("checkin", lambda: step_checkin(cli)),
                         ("streak", lambda: step_streak(cli))):
            try:
                steps.append(fn())
            except WbipcError as exc:
                steps.append({"name": name, "status": "error", "detail": str(exc)})

        if args.no_travel:
            steps.append({"name": "travel", "status": "skipped", "detail": "已按参数跳过"})
        else:
            try:
                steps.append(step_travel(cli, do_depart=not args.no_depart))
            except WbipcError as exc:
                steps.append({"name": "travel", "status": "error", "detail": str(exc)})

        for step in steps:
            allowed = LEGAL.get(step["name"], set())
            if step["status"] not in allowed:
                anomalies.append("%s 返回非法状态：%r" % (step["name"], step["status"]))
            if step["status"] == "error" and step.get("detail"):
                anomalies.append("%s 失败：%s" % (step["name"], step["detail"]))

        streak = next((s for s in steps if s["name"] == "streak"), {})
        balance = streak.get("balance")

        payload = {
            "ok": not anomalies,
            "version": VERSION,
            "channel": "wbipc",
            "steps": steps,
            "anomalies": anomalies,
            "balance": balance,
            "streak_days": streak.get("days"),
            "elapsed": round((dt.datetime.now() - started).total_seconds(), 2),
        }
        emit(args, payload)
        write_log("%s | status=%s | checkin=%s | travel=%s | balance=%s"
                  % (now_text(), "ok" if payload["ok"] else "anomaly",
                     steps[0]["status"] if steps else "-",
                     next((s["status"] for s in steps if s["name"] == "travel"), "-"),
                     balance))
        return 0 if payload["ok"] else 1
    finally:
        cli.close()


def emit(args, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not payload.get("ok") and not payload.get("steps"):
        print("❌ %s" % payload.get("error", "执行失败"))
        return
    if payload.get("probe"):
        print("✅ 探测通过｜今天%s｜连签 %s 天｜余额 %s"
              % ("已签" if payload.get("today_checked_in") else "未签",
                 payload.get("streak_days"), payload.get("balance")))
        return

    icons = {"success": "✅", "already_checked": "☑️", "ready": "ℹ️", "locked": "🔒",
             "departed": "🚀", "traveling": "🚄", "claimed": "🎁", "idle_limit": "⏳",
             "no_buddy": "⚠️", "skipped": "⏭", "error": "❌"}
    labels = {"checkin": "每日签到", "streak": "连签进度", "travel": "猫猫旅行"}
    print("WorkBuddy 加油站 %s\n" % now_text())
    for step in payload.get("steps", []):
        icon = icons.get(step["status"], "·")
        text = labels.get(step["name"], step["name"])
        if step["name"] == "checkin":
            text += "：" + {"success": "签到成功", "already_checked": "今天已签",
                            "skipped": "跳过"}.get(step["status"], step["status"])
        elif step["name"] == "streak":
            text += "：%s 天" % step.get("days", 0)
            if step.get("next_tier"):
                text += "（下一档 %s，还差 %s 天）" % (step["next_tier"], step.get("remaining"))
        elif step["name"] == "travel":
            detail = {"departed": "已派遣去%s" % (step.get("location") or ""),
                      "traveling": "在途，%s 回来" % step.get("arrive_at", "-"),
                      "claimed": "已领取奖励",
                      "idle_limit": "今日次数已用完",
                      "no_buddy": "未领取 Buddy",
                      "skipped": "已跳过"}.get(step["status"], step["status"])
            text += "：" + (step.get("detail") or detail)
        print("  %s %s" % (icon, text))
    if payload.get("anomalies"):
        print("\n⚠️ 异常：")
        for item in payload["anomalies"]:
            print("   - %s" % item)
    if payload.get("balance") is not None:
        print("\n当前加油站余额：%s" % payload["balance"])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="checkin.py", description="WorkBuddy 加油站每日签到（本机 IPC，零凭据）")
    parser.add_argument("--doctor", action="store_true", help="环境自检")
    parser.add_argument("--probe", action="store_true", help="只读探测，不发起任何写请求")
    parser.add_argument("--json", action="store_true", help="结构化输出")
    parser.add_argument("--no-travel", action="store_true", help="跳过猫猫旅行")
    parser.add_argument("--no-depart", action="store_true", help="只领取已到达的奖励，不发起新派遣")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
