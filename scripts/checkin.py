"""WorkBuddy「Buddy 加油站」每日签到 / 连签进度 / 猫猫旅行。

走客户端本机 IPC（wb.request 管道），由 WorkBuddy 桌面端用自己的登录态代发
HTTP 请求 —— 本脚本不读取、不保存、不打印任何凭据。

所有领取类操作幂等，重复执行不会重复领取。

退出码
    0  成功（含「今天已签到」这类幂等跳过）
    1  接口 / 网络错误
    2  参数错误
    3  IPC 通道不可用（客户端没开、未登录，或协议版本不匹配）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wbipc import WbipcClient, WbipcError, config_dir, discovery_path  # noqa: E402

VERSION = "1.1.0"
SCHEMA = 1

PATH_ACTIVITY = "/v2/billing/meter/checkin-activity-status"
PATH_CHECKIN = "/v2/billing/meter/daily-checkin"
PATH_TRAVEL = "/activity/growth/buddy/travel"

# 签到接口的业务返回码。10001 是「今天已签到」的实测值。
# 1001 / 1002 / 1003 是客户端映射表里的同类语义，一并兼容，避免版本差异误判。
BIZ_ALREADY = {1001, 10001}
BIZ_NOT_ELIGIBLE = 1002
BIZ_ENDED = 1003

# 旅行地点。收益四地相同，缺省随机，避免每天都去同一处。
TRAVEL_LOCATIONS = (1, 2, 3, 4)

# 结果契约里的合法状态。`error` 是合法状态，但一定同时记入 anomalies。
LEGAL = {
    "checkin": {"success", "already_checked", "pending", "skipped", "error"},
    "streak": {"ready", "locked", "skipped", "error"},
    "travel": {"departed", "claimed", "traveling", "arrived", "idle_limit", "idle",
               "no_buddy", "skipped", "error"},
}

BALANCE_KEYS = ("total_credits", "balance", "points_balance", "credits", "total_credit")


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


def pick_balance(data: dict):
    """余额字段在不同版本 / 不同接口里名字不一致，按优先级挨个试。"""
    if not isinstance(data, dict):
        return None
    for key in BALANCE_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def ts_text(value) -> str:
    try:
        return dt.datetime.fromtimestamp(int(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def minutes_left(value):
    try:
        return max(0, int((int(value) - time.time()) // 60))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 业务

def read_activity(cli: WbipcClient) -> dict:
    return unwrap(cli.http("POST", PATH_ACTIVITY, {}))


def step_checkin(cli: WbipcClient, activity: dict) -> dict:
    """签到。

    先看状态：状态已经显示今天签过，就不发领取请求 —— 省掉一次必然 400 的调用，
    也避免把「业务拒绝」当成正常路径来依赖。状态过期时仍按业务码兜底。
    """
    out = {"name": "checkin", "status": "skipped"}

    if activity.get("today_checked_in"):
        out["status"] = "already_checked"
        out["detail"] = "活动状态显示今天已签到，未发起领取请求"
        out["credit"] = activity.get("today_credit")
        return out

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
    out["status"] = "success"
    out["credit"] = data.get("credit") or data.get("today_credit") or data.get("daily_credit")
    if data.get("streak_days"):
        out["streak_days"] = data["streak_days"]
    return out


def step_streak(activity: dict, checkin: dict) -> dict:
    streak = checkin.get("streak_days") or activity.get("streak_days") or 0
    out = {
        "name": "streak",
        "status": "ready",
        "days": streak,
        "today_checked_in": bool(activity.get("today_checked_in")),
        "week_checkin_days": activity.get("week_checkin_days"),
        "bonus_days": activity.get("streak_bonus_days") or 0,
        "bonus_credit": activity.get("streak_bonus_credit") or 0,
        "active": bool(activity.get("active")),
        "balance": pick_balance(activity),
        "end_time": activity.get("end_time"),
        "today_credit": activity.get("today_credit"),
    }
    nxt = activity.get("next_streak_day") or 0
    if nxt:
        out["next_tier"] = "%dd" % nxt
        out["remaining"] = max(0, nxt - streak)
    if not out["active"]:
        out["status"] = "locked"
        out["detail"] = "活动未开启或已结束"
    return out


def step_travel(cli: WbipcClient, do_depart: bool = True,
                location: int | None = None, readonly: bool = False) -> dict:
    """猫猫旅行：先领后派。

    派遣前必查 daily_limit_reached —— 达上限一个写请求都不发。
    arrived 状态会一直保留，下次运行自动补领，不会丢积分。
    """
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
        "arrive_at": ts_text(data.get("arrive_at")),
    }

    if not buddy_id:
        out["status"] = "no_buddy"
        out["detail"] = "账号下还没有 Buddy，先去客户端「成长计划」领取一只"
        return out

    if state == "arrived":
        if readonly:
            out["status"] = "arrived"
            out["detail"] = "已到达，待领取（只读模式不领）"
            return out
        resp = cli.http("POST", PATH_TRAVEL + "/claim", {})
        if resp.get("code") == 0:
            out["status"] = "claimed"
            reward = (resp.get("data") or {}).get("reward_credit") or out.get("reward_credit")
            out["reward_credit"] = reward
            out["detail"] = "已领取到达奖励"
        else:
            out["status"] = "error"
            out["detail"] = resp.get("msg") or "领取失败"
        return out

    if state == "traveling":
        out["status"] = "traveling"
        left = minutes_left(data.get("arrive_at"))
        if left is not None:
            out["minutes_left"] = left
        return out

    if state == "idle":
        if data.get("daily_limit_reached"):
            out["status"] = "idle_limit"
            out["detail"] = "今天的派遣次数已用完，明天再来"
            return out
        if not do_depart:
            out["status"] = "idle"
            out["detail"] = "空闲中，已按参数跳过派遣"
            return out
        target = location or random.choice(TRAVEL_LOCATIONS)
        resp = cli.http("POST", PATH_TRAVEL + "/depart", {"location_id": target})
        if resp.get("code") == 0:
            body = resp.get("data") or {}
            out["status"] = "departed"
            out["location_id"] = target
            out["location"] = (body.get("location") or {}).get("name") or out.get("location")
            out["arrive_at"] = ts_text(body.get("arrive_at"))
            out["reward_credit"] = body.get("reward_credit") or out.get("reward_credit")
        else:
            out["status"] = "error"
            out["detail"] = resp.get("msg") or "派遣失败"
        return out

    out["status"] = "error"
    out["detail"] = "未知旅行状态：%s" % state
    return out


def probe_steps(cli: WbipcClient) -> tuple[list, list]:
    """只读：状态 + 旅行，不发任何写请求。"""
    steps, anomalies = [], []
    try:
        activity = read_activity(cli)
        steps.append({
            "name": "checkin",
            "status": "already_checked" if activity.get("today_checked_in") else "pending",
            "detail": "今天已签到" if activity.get("today_checked_in") else "今天还没签到",
            "credit": activity.get("today_credit"),
        })
        steps.append(step_streak(activity, {}))
    except WbipcError as exc:
        steps.append({"name": "checkin", "status": "error", "detail": str(exc)})
        anomalies.append("checkin 失败：%s" % exc)
        steps.append({"name": "streak", "status": "error", "detail": str(exc)})
        anomalies.append("streak 失败：%s" % exc)

    try:
        steps.append(step_travel(cli, do_depart=False, readonly=True))
    except WbipcError as exc:
        steps.append({"name": "travel", "status": "error", "detail": str(exc)})
        # 旅行不可用不影响签到结论，只在 anomalies 里提一句
        anomalies.append("travel 不可用：%s" % exc)

    return steps, anomalies


# ---------------------------------------------------------------- 自检

def doctor() -> int:
    print("wb-buddy-checkin %s ｜ 自检 %s\n" % (VERSION, now_text()))

    path = discovery_path()
    print("  发现文件：%s" % path)
    print("  %s 存在" % ("✅" if os.path.exists(path) else "❌"))
    print("  Python %s" % sys.version.split()[0])

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
                 data.get("streak_days") or 0, pick_balance(data)))
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
            if not buddy:
                print("     没有 Buddy 时旅行步骤会返回 no_buddy，属账号状态而非故障")
    finally:
        cli.close()

    print("\n结论：✅ 环境正常")
    return 0


def selftest() -> int:
    """离线自检：不联网、不读登录态、不依赖客户端。

    只验证协议实现里最容易出错、也最不能出错的部分：票据标识、被 HMAC 覆盖的
    字节串结构、证明的可复现性与敏感性、NDJSON 拆帧。
    """
    import base64
    import hashlib
    import hmac
    import struct

    import wbipc

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 1. base64url 无填充
    raw = bytes(range(32))
    enc = wbipc._b64url(raw)
    check("base64url 无填充且可逆",
          "=" not in enc and "+" not in enc and "/" not in enc
          and base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)) == raw,
          enc[:12] + "...")

    # 2. ticket_id 形状
    tid = wbipc.ticket_id("dummy-ticket")
    check("ticket_id 为 16 位十六进制", len(tid) == 16 and all(c in "0123456789abcdef" for c in tid), tid)

    # 3. transcript 长度前缀
    tr = wbipc._transcript("client", "/tmp/x.sock", "cn", "sn")
    first = struct.unpack(">I", tr[:4])[0]
    check("transcript 首段为 7 字节角色前缀",
          first == 7 and tr[4:11] == b"wbipc-c" and len(tr) > 11, "len=%d" % len(tr))

    # 4. 证明可复现
    p1 = wbipc.compute_proof("tk", "client", "/e", "cn", "sn")
    p2 = wbipc.compute_proof("tk", "client", "/e", "cn", "sn")
    check("同一输入证明可复现", p1 == p2, p1[:10] + "...")

    # 5. 证明对输入敏感（nonce / 角色 / endpoint 任一变化都必须变）
    variants = {
        "nonce": wbipc.compute_proof("tk", "client", "/e", "cn2", "sn"),
        "role": wbipc.compute_proof("tk", "server", "/e", "cn", "sn"),
        "endpoint": wbipc.compute_proof("tk", "client", "/e2", "cn", "sn"),
        "ticket": wbipc.compute_proof("tk2", "client", "/e", "cn", "sn"),
    }
    check("证明对 nonce/角色/endpoint/ticket 敏感",
          all(v != p1 for v in variants.values()),
          "四路均不同" if all(v != p1 for v in variants.values()) else str(variants))

    # 6. 证明就是 HMAC-SHA256 的 base64url
    expect = hmac.new(b"tk", wbipc._transcript("client", "/e", "cn", "sn"),
                      hashlib.sha256).digest()
    check("证明算法为 HMAC-SHA256", p1 == wbipc._b64url(expect), "")

    # 7. NDJSON 拆帧：粘包 + 半包
    tx = wbipc._Transport.__new__(wbipc._Transport)
    tx._buf = b'{"a":1}\n{"b":2}\n{"c":'
    tx._fd = tx._sock = None
    ok = tx.recv(1.0) == {"a": 1} and tx.recv(1.0) == {"b": 2}
    check("NDJSON 逐行拆帧", ok, "剩余缓冲 %r" % tx._buf[:10])

    width = max(len(n) for n, _, _ in checks)
    passed = 0
    print("wb-buddy-checkin %s ｜ 离线自检（不联网、不读登录态）\n" % VERSION)
    for name, ok, detail in checks:
        print("  %s %-*s %s" % ("✅" if ok else "❌", width, name, detail))
        passed += 1 if ok else 0
    print("\n%d/%d 项通过" % (passed, len(checks)))
    if passed != len(checks):
        print("结论：❌ 协议实现自检未通过")
        return 1
    print("结论：✅ 协议实现自检通过")
    return 0


# ---------------------------------------------------------------- 主流程

def connect(retries: int = 2) -> WbipcClient:
    """连接 + 握手。客户端重启会轮换票据，这里重读 endpoint.json 重试一次。"""
    last = None
    for attempt in range(retries):
        try:
            cli = WbipcClient(timeout=25.0)
            cli.connect()
            return cli
        except WbipcError as exc:
            last = exc
            if exc.code not in ("E_AUTH", "E_NOT_CONNECTED", "E_NO_ENDPOINT") or attempt == retries - 1:
                raise
            time.sleep(0.8)
    raise last  # pragma: no cover


def run(args) -> int:
    if args.self_test:
        return selftest()
    if args.doctor:
        return doctor()

    if args.command == "travel":
        mode = "travel"
    elif args.probe or args.check_only:
        mode = "probe"
    else:
        mode = "full"

    started = dt.datetime.now()
    try:
        cli = connect()
    except WbipcError as exc:
        payload = {
            "schema": SCHEMA, "ok": False, "version": VERSION, "mode": mode,
            "channel": "wbipc", "error": "%s: %s" % (exc.code, exc),
            "steps": [], "anomalies": [],
        }
        emit(args, payload)
        write_log("%s | status=no_channel | mode=%s | code=%s"
                  % (now_text(), mode, exc.code))
        return 3

    steps: list[dict] = []
    anomalies: list[str] = []
    try:
        if mode == "travel":
            try:
                steps.append(step_travel(cli, do_depart=bool(args.travel_auto),
                                         location=args.location,
                                         readonly=not args.travel_auto))
            except WbipcError as exc:
                steps.append({"name": "travel", "status": "error", "detail": str(exc)})
        elif mode == "probe":
            steps, anomalies = probe_steps(cli)
        else:
            activity: dict = {}
            try:
                activity = read_activity(cli)
                checkin = step_checkin(cli, activity)
                steps.append(checkin)
                steps.append(step_streak(activity, checkin))
            except WbipcError as exc:
                if not steps:
                    steps.append({"name": "checkin", "status": "error", "detail": str(exc)})
                    steps.append({"name": "streak", "status": "error", "detail": str(exc)})
                else:
                    steps.append({"name": "streak", "status": "error", "detail": str(exc)})

            if args.no_travel:
                steps.append({"name": "travel", "status": "skipped", "detail": "已按参数跳过"})
            else:
                try:
                    steps.append(step_travel(cli, do_depart=not args.no_depart,
                                             location=args.location))
                except WbipcError as exc:
                    steps.append({"name": "travel", "status": "error", "detail": str(exc)})

        for step in steps:
            allowed = LEGAL.get(step["name"], set())
            if step["status"] not in allowed:
                anomalies.append("%s 返回非法状态：%r" % (step["name"], step["status"]))
            if step["status"] == "error":
                detail = step.get("detail") or "未知错误"
                anomalies.append("%s 失败：%s" % (step["name"], detail))

        streak = next((s for s in steps if s["name"] == "streak"), {})
        travel = next((s for s in steps if s["name"] == "travel"), {})
        balance = streak.get("balance")
        payload = {
            "schema": SCHEMA,
            "ok": not anomalies,
            "version": VERSION,
            "mode": mode,
            "channel": "wbipc",
            "steps": steps,
            "anomalies": anomalies,
            "balance": balance,
            "streak_days": streak.get("days"),
            "today_credit": (next((s for s in steps if s["name"] == "checkin"), {})
                             .get("credit")),
            "travel_reward": travel.get("reward_credit"),
            "elapsed": round((dt.datetime.now() - started).total_seconds(), 2),
        }
        if args.json:
            payload["generated_at"] = now_text()
        emit(args, payload)
        write_log("%s | status=%s | mode=%s | checkin=%s | travel=%s | balance=%s"
                  % (now_text(), "ok" if payload["ok"] else "anomaly", mode,
                     next((s["status"] for s in steps if s["name"] == "checkin"), "-"),
                     travel.get("status", "-"), balance))
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

    icons = {"success": "✅", "already_checked": "☑️", "pending": "○", "ready": "ℹ️",
             "locked": "🔒", "departed": "🚀", "traveling": "🚄", "claimed": "🎁",
             "arrived": "📬", "idle": "🐾", "idle_limit": "⏳", "no_buddy": "⚠️",
             "skipped": "⏭", "error": "❌"}
    labels = {"checkin": "每日签到", "streak": "连签进度", "travel": "猫猫旅行"}
    print("WorkBuddy 加油站 %s%s\n"
          % (now_text(),
             {"probe": "（只读探测）", "travel": "（猫猫旅行）"}.get(payload.get("mode", ""), "")))

    for step in payload.get("steps", []):
        icon = icons.get(step["status"], "·")
        text = labels.get(step["name"], step["name"])
        if step["name"] == "checkin":
            text += "：" + {"success": "签到成功 +%s" % (step.get("credit") or "?"),
                            "already_checked": "今天已签",
                            "pending": "今天还没签",
                            "skipped": "跳过",
                            "error": step.get("detail", "失败")}.get(step["status"], step["status"])
        elif step["name"] == "streak":
            text += "：%s 天" % step.get("days", 0)
            if step.get("next_tier"):
                text += "（下一档 %s，还差 %s 天）" % (step["next_tier"], step.get("remaining"))
            if step.get("status") == "locked":
                text += "｜活动未开启"
        elif step["name"] == "travel":
            detail = {"departed": "已派遣去%s，%s 回来"
                                    % (step.get("location") or "?", step.get("arrive_at", "-")),
                      "traveling": "在途，%s 回来%s"
                                   % (step.get("arrive_at", "-"),
                                      "（约 %d 分钟）" % step["minutes_left"]
                                      if step.get("minutes_left") is not None else ""),
                      "claimed": "已领取奖励%s"
                                 % (" +%s" % step["reward_credit"]
                                    if step.get("reward_credit") else ""),
                      "arrived": "已到达，待领取",
                      "idle": "空闲中",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="checkin.py",
        description="WorkBuddy 加油站：每日签到 / 连签进度 / 猫猫旅行（本机 IPC，零凭据）")
    parser.add_argument("command", nargs="?", choices=["travel"],
                        help="可选子命令：travel 只跑猫猫旅行（默认只读，加 --travel-auto 才派遣）")
    parser.add_argument("--doctor", action="store_true", help="环境自检")
    parser.add_argument("--probe", action="store_true", help="只读探测，不发起任何写请求")
    parser.add_argument("--status", "--check-only", dest="check_only", action="store_true",
                        help="只读查询签到 / 连签 / 旅行状态（--probe 的别名）")
    parser.add_argument("--json", action="store_true", help="结构化输出")
    parser.add_argument("--no-travel", action="store_true", help="跳过猫猫旅行")
    parser.add_argument("--no-depart", action="store_true",
                        help="只领取已到达的奖励，不发起新派遣")
    parser.add_argument("--travel-auto", action="store_true",
                        help="配合 travel 子命令：允许领取并派遣")
    parser.add_argument("--location", type=int, choices=list(TRAVEL_LOCATIONS),
                        help="派遣地点 1-4，缺省随机")
    parser.add_argument("--self-test", action="store_true",
                        help="离线自检协议实现（不联网、不读登录态）")
    parser.add_argument("--version", action="version", version="wb-buddy-checkin %s" % VERSION)
    return parser


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
