# 本地逐条审批浏览器服务

状态：`v1alpha` 库 API。`latex_word_review.review_server` 把 sealed ChangeSet 与当前
ApprovalSet 呈现为一个只绑定 `127.0.0.1` 的临时网页。它只是 ApprovalSet 状态机的
本地浏览器前端：不会读取、写入或应用 `.tex`，不会生成 PatchPlan，也不会调用
planner/applier。

## 启动方式

普通用户使用 CLI 入口：

```console
latex-word-review approve serve changeset.json approval-r1.json approvals/ --open-browser
```

服务会把后续 revision 以 `approval-r2.json`、`approval-r3.json` 等新文件
发布到给定目录；它不会覆盖现有文件。默认由操作系统分配端口，只有
显式给出 `--open-browser` 时才请求系统浏览器打开页面。

库 API 为：

调用方必须为可能产生的每个新 revision 预先给出一个显式、父目录已存在、尚未使用的
`.json` 路径：

```python
from pathlib import Path

from latex_word_review.review_server import create_review_server

server = create_review_server(
    changeset,
    current_approval,
    approval_output_paths={
        2: Path("review/approval-r2.json"),
        3: Path("review/approval-r3.json"),
        4: Path("review/approval-r4.json"),
    },
    host="127.0.0.1",
    port=0,
)
print(server.origin)  # 例如 http://127.0.0.1:53142
try:
    server.serve_forever()
finally:
    server.server_close()
```

`port=0` 让操作系统选择空闲端口。`host` 只接受字面值 `127.0.0.1`；`0.0.0.0`、
`localhost`、IPv6 和任意局域网/公网地址均在绑定前拒绝。输出映射不完整时，相应状态
转换会失败，当前内存状态保持不变。目标文件已存在或为链接时也不会覆盖。

CLI、Codex Skill 或桌面编排层均复用同一个库状态机，不得绕过
`approval.record_decision()`、`approval.finalize_approval_set()` 与
`approval.write_approval_json()` 的完整性和 no-clobber 约束。

## 用户如何逐条操作

打开 `server.origin` 后，页面默认定位到第一条 pending change；也可以在导航列表中
选择任意 change。每条页面明确显示：

- change ID、类型、作者和时间；
- before、after；
- SourceMap 定位信息；
- raw event IDs、resolution 与 change fingerprint 证据；
- 当前 decision。

用户对当前项选择以下一种结果：

| 页面动作 | ApprovalSet decision | 说明 |
| --- | --- | --- |
| Accept | `accepted` | 接受返回文本；只记录意图 |
| Accept edited | `accepted_with_edit` | 使用用户填写的最终文本 |
| Reject | `rejected` | 保留 LaTeX 原文 |
| Manual | `manual` | 留给人工在后续副本中处理 |
| Conflict | `conflict` | 明确记录无法安全自动决策 |

还可以填写 reason。除 `accepted_with_edit` 外，提交非空 final text 会被拒绝。每次有效
决策都从当前 ApprovalSet 生成下一 revision，并立即以 canonical JSON 写入该 revision
的显式新路径。服务从不原地覆盖旧 ApprovalSet。

所有 change 已决定后，用户可以按 **Finalize approval**。状态机仍会重新检查完整性；
存在 pending 项时返回冲突且不写盘。final ApprovalSet 只代表审批完成，不代表补丁已
计划或应用。final 后页面只读，不再提供决策表单。零变更 ChangeSet 也可以直接安全
finalize。

## 并发与陈旧页面

每个表单同时绑定当前 `revision` 与 ApprovalSet payload SHA-256。服务在同一锁内再次
核对两者、运行状态机并执行 no-clobber 写盘；只有写盘成功后才更新内存状态。因此两个
并发请求若从同一页面提交，最多一个能够发布新 revision，另一个以 HTTP 409 拒绝。

浏览器中打开很久的页面也按同样规则处理。收到 409 后应重新加载页面，查看刚发布的
决策，再决定是否发起新的显式状态转换；服务不会自动合并或覆盖。

## HTTP 安全边界

该服务不是通用 Web 应用，也不应被反向代理、端口转发或暴露到局域网/公网。创建服务
时生成相互独立的高熵 session token 与 CSRF token。GET 只在 Host 精确等于实际
`127.0.0.1:port` 时返回页面，并设置 `HttpOnly; SameSite=Strict` session cookie。
POST 还必须同时满足：

- Host 与本服务精确匹配；
- Origin 精确等于本服务的 `http://127.0.0.1:port`；
- session cookie 精确匹配且不重复；
- CSRF hidden field 精确匹配；
- 若存在 `Sec-Fetch-Site`，其值必须为 `same-origin`；
- 请求为 origin-form 的固定 `/decision` 或 `/finalize` 路径。

服务不发送 CORS 许可。所有响应包含 `default-src 'none'`、`form-action 'self'`、
`frame-ancestors 'none'` 的 CSP，以及 no-store、nosniff、DENY、no-referrer、COOP、
CORP 和禁用敏感浏览器能力的安全头。页面中的 ChangeSet、ApprovalSet 与错误文本均经
HTML attribute-safe escaping，不把证据作为可执行 HTML 插入。

POST 只接受精确的 `application/x-www-form-urlencoded`。请求体上限为 32 KiB，单字段
解码后上限为 16 KiB；Transfer-Encoding、非法百分号编码、重复字段、未知字段、缺失
字段和超出字段数限制均 fail-closed。请求路径拒绝绝对 URI、query、fragment、百分号、
反斜杠和 `..`，且没有任何文件系统读取端点。HEAD、OPTIONS、PUT、PATCH、DELETE
统一返回 405。

这些措施防范浏览器跨站请求、Host header 攻击、页面数据 XSS、陈旧提交和意外覆盖；
它们不隔离同一用户账户下已经能主动连接 loopback 的恶意本地进程。敏感审稿数据仍应
只在可信本机和短生命周期会话中处理，服务用毕后立即关闭。

## 持久化与错误结果

服务唯一允许的新持久化产物是调用方显式列出的 ApprovalSet JSON。写盘继续复用
`write_approval_json()` 的完整 ChangeSet/ApprovalSet 绑定校验、同文件系统临时文件、
`fsync` 和原子 no-clobber 发布。下列 HTTP 结果具有稳定含义：

| HTTP | 含义 |
| ---: | --- |
| 303 | 新 approval revision 已安全发布；重新 GET 当前页 |
| 400/415 | Schema、表单、决策值、输出路径或 no-clobber 条件不满足 |
| 403 | Origin、session、CSRF 或 Fetch Metadata 校验失败 |
| 409 | revision/hash 陈旧、change 未知或审批尚不能 finalize |
| 413 | 请求体超过固定上限 |
| 421 | Host 不匹配 |

错误页面不回显内部路径或异常详情。无论成功还是失败，此模块都不取得 LaTeX 来源根、
PatchPlan 或 apply 目标，因此不能越权修改论文。
