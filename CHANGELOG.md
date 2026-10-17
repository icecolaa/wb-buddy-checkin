# 变更记录

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
