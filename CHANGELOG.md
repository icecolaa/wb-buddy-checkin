# 变更记录

## 1.1.1

修一个会把正常状态误报成异常的问题：

- **修正 `no_buddy` 误判**：实测发现 `/activity/growth/buddy/travel/status` 在**没有进行中的旅行时**
  会把 `buddy_id` 归零（今日旅行已领完、猫在家歇着都会归零）。旧逻辑据此就报 `no_buddy`，
  导致每天定时任务的汇报都变成「你还没领猫」的假警报。
  现在改为回查权威接口 `/activity/growth/buddy/info` 才判定，并在 `buddy_id` 为 0 时
  回填真实 Buddy（`buddy_name` / `buddy_rarity`）。
- **`--doctor` 显示猫的名字**：`龙焰喵（SSR 技术霸主）` 这种形式；并对「ID 归零但有猫」给出说明行。
- 文档补记这条服务端行为，避免被当成 bug 重复排查。

## 1.1.0

参考同类实现的实际使用反馈，补齐「能不能放心交给 Agent 每天跑」这一层：

- **新增 `--check-only` / `--status`（同 `--probe`）**：一次性只读给出签到、连签、余额、旅行四类信息，零写请求。
- **新增 `travel` 子命令**：只看旅行状态；加 `--travel-auto` 才领取与派遣，不带时完全只读。
- **新增 `--location 1-4`**：指定派遣地点，缺省随机（四地收益相同）。
- **签到改为状态优先**：`checkin-activity-status` 显示今天已签就**不再发领取请求**，省掉一次必然 400 的调用；业务码 `10001` 仍作兜底。
- **余额字段兜底**：`total_credits` / `balance` / `points_balance` / `credits` 依次尝试，避免版本差异导致余额为空。
- **连接自动重试一次**：客户端重启会轮换 endpoint 与票据，`E_AUTH` / `E_NOT_CONNECTED` 时重读 `endpoint.json` 重连。
- **新增 `--self-test`**：离线自检 base64url、ticket_id、transcript 长度前缀、证明的可复现与敏感性、NDJSON 拆帧，7 项全过才返回 0（不联网、不读登录态）。
- **结果契约补 `schema` / `mode` / `generated_at`**，新增 `pending` / `arrived` / `idle` 三个合法状态；`error` 明确为「合法但必记入 anomalies」，不再重复报两条异常。
- **文档重构**：SKILL.md 补齐触发词、三种模式、接口事实、自动化搭建、排错表；新增 `references/protocol.md`（IPC 协议备忘）与 `references/examples.md`（自动化提示词与输出样例）。

## 1.0.0

- 首个版本：本机 IPC 直连（NDJSON + HMAC-SHA256 四帧握手 + JSON-RPC），
  签到 / 连签 / 猫猫旅行三步全幂等，零第三方依赖，全程不接触凭据。
