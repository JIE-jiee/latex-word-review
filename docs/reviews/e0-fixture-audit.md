# E0 public fixture 独立审计

- 审计日期：2026-07-16
- 审计对象：`tests/fixtures/e0-minimal-paper/`、`scripts/build_e0_public_fixture.py`、`scripts/qa_e0_public_fixture.py`、`scripts/e0_inspect_docx.py`
- 审计性质：独立于样例生成者的复核；未读取或操作私人论文，未访问远程仓库。

> **历史快照，已被后续审计取代。** 本文保留的是 clean-room 生成器落地之前的
> fail-closed 发现，不能作为当前发布状态。许可证、公开状态、仓库外 Documents Skill
> 依赖和依赖锁问题已由
> [`e0-cleanroom-generator.md`](e0-cleanroom-generator.md)、当前 `provenance.json`、
> `pyproject.toml` 与 `uv.lock` 取代；逐页 Word/LibreOffice 视觉验收仍应以最新 QA/
> 发布门禁记录为准。以下“当前”“尚无”“必须完成”均指本次审计时点。

## 结论

E0 样例的内容、结构、隐私扫描和静态 oracle 均可通过，但在该历史审计时点
**不具备公开发布条件**。当时的最终 QA 结果有意区分为：

- fixture integrity：`pass`
- structure/oracle/privacy：`pass`
- release readiness：`blocked`
- 总状态：`fail`（发布门禁失败，而非样例内容损坏）

当时共有四个阻断项：样例许可证未确定、公开发布状态仍为 blocked、视觉验收缺少
LibreOffice/Word 实测、生成器依赖仓库外且不可再分发的 Documents Skill。前三项中的
许可/状态/生成器问题已在后续 clean-room 报告中闭环；视觉证据不由本文更新或推断。

## 主要发现

### [P0，历史] 许可证与发布状态未闭环

`provenance.json` 仍写明 `license: PENDING_USER_CONFIRMATION` 和 `public_release_status: blocked_until_fixture_license_is_confirmed`。虽然来源声明为完全合成、作者允许再分发，仍不能以此替代明确的开源许可证。项目根目录也尚无 `LICENSE`。

处理：未擅自替用户选择许可证；QA 现在把占位许可证和 blocked 状态作为发布失败，而不再给出容易误解的总绿灯。

### [P0，历史] 生成器不是 clean-clone 可重现的公开实现

`build_e0_public_fixture.py` 强制要求 `--documents-skill-root`，并动态加载三个本机 Skill helper；`provenance.json` 还固定了本机 Skill 版本。该 Skill 的许可条款不允许抽取、复制或向第三方分发其实现，因此既不能把它作为 GitHub 用户必备依赖，也不能直接复制 helper 进仓库。

处理：本次没有复制或改写受限实现。QA 现在会检测该外部依赖并阻断发布。后续必须在仓库内独立实现所需的最小 OOXML、隐私清理和表格几何能力，或重构生成流程。

### [P0，历史状态需查最新门禁] 视觉验收尚未完成

provenance 明确记录 `blocked_missing_libreoffice`。结构 smoke test 不能证明分页、公式、图表、表格和批注在 Word/LibreOffice 中视觉正确。

处理：保留阻断，不把“缺少渲染器”误报为 pass。

### [P1] 旧 oracle 可在部分错误样例上误通过

原 QA 仅按 `(kind, w:id)` 建字典，重复 id 会被覆盖；没有核对实际 revision id 集合是否与 oracle 完全一致；格式修订只核对作者和时间，不核对目标文本及 bold 前后状态；移动修订只数 range marker，不核对 `range_id`、作者、时间和 move name。

处理：已修复。现在执行 exact identity/uniqueness 检查，并核对：

- `critical observation` 的 run-format 上下文；
- `bold: false -> true`；
- move from/to 的 range start/end id、作者、时间和共享非空 name；
- comment id 与 start/end/reference anchor 的精确集合。

独立性判断：`expected-changeset.json` 是静态人工 oracle，生成脚本不生成或改写它，且其哈希列入 provenance，因此不是“生成器自证”。本次补强的是 oracle 覆盖面，而不是让生成器生成期望值。

### [P1] ZIP/XML 防护存在可绕过点

原 observer 会把 ZIP member 名称转成集合，导致重复 member 被隐藏；未拒绝加密或非常规压缩；未做完整 CRC；只扫描 XML 前 4096 字节中的 DTD/entity，声明可通过前置填充绕过；无单 XML part 大小上限。

处理：已修复。现在拒绝重复/非规范路径、加密 member、非 stored/deflated 压缩，执行 CRC 检查，限制单 XML part 为 16 MiB，并扫描完整且已限长的 XML（兼顾普通 UTF-16/32 NUL 布局）。

### [P1] QA 路径与子进程编码边界不完整

原 QA 允许输出到工程外；observer 子进程使用平台默认文本编码且无 timeout；在原始包安全检查前先调用 `python-docx`。

处理：已修复。fixture 只允许位于 `tests/fixtures/`，报告只允许写入 `build/`；子进程固定 UTF-8、30 秒 timeout；bounded observer 先于高层解析器执行。observer 输出使用 ASCII-safe JSON 转义，避免 Windows locale 差异。

### [P2] 隐私扫描覆盖不足

原扫描对 `w:vanish` 依赖固定前缀，路径规则漏掉 `C:/`、UNC、`file://`、`/tmp` 等；未检查 Manager、HyperlinkBase、评论 initials、rsid、VBA/ActiveX/OLE 和现代 comments identity parts。

处理：已补齐上述检查。当前两个 DOCX 均通过加强后的隐私扫描，未发现个人路径、邮件、外链、隐藏文本、私有作者元数据或嵌入对象。

### [P2，历史] 依赖锁定仍缺失

仓库没有 `pyproject.toml` 或 `requirements*.txt`；provenance 中的版本记录不是可安装的依赖锁。本机版本也已与记录值不同，因此即使移除 Skill 依赖，clean-clone 重建仍缺少环境契约。`build/` 已被 `.gitignore` 排除，这一点正确，公共仓库不应提交本地 QA 临时产物。

## 本次修改

- `scripts/e0_inspect_docx.py`
  - 加固 ZIP member、CRC、压缩方法、XML 大小和 DTD/entity 检查；
  - 对 run-format 观察结果增加文本与 bold 前后状态；
  - 收紧 story part 识别并输出跨平台安全 JSON。
- `scripts/qa_e0_public_fixture.py`
  - 加入 JSON 重复键、路径边界、UTF-8/timeout；
  - 加强 revision、move、comment 和 format oracle；
  - 加强隐私/元数据扫描；
  - 增加完整 manifest、fixture id/oracle 声明检查；
  - 将完整性状态与发布就绪状态分开报告。

未修改 fixture 二进制、expected oracle、provenance 声明或生成脚本；没有重新生成 DOCX，因为当前生成链依赖不可分发的外部 Skill。

## 回归测试

1. `python -m py_compile scripts/qa_e0_public_fixture.py scripts/e0_inspect_docx.py`：通过。
2. 对原 base/returned DOCX 运行 observer：通过。
3. 最终 fixture QA 输出到 `build/e0-fixture-audit/final/`：结构、oracle、privacy、integrity 全部通过；仅 release readiness 按预期阻断。
4. 构造重复 `word/document.xml` 的 DOCX：observer 以退出码 2 拒绝，错误为 duplicate member。
5. 构造在 5000 字节填充后放置 DTD/entity 的 DOCX：observer 以退出码 2 拒绝。
6. 构造移除当前 bold 的格式修订 DOCX：新 oracle 精确失败于 `CHG-FMT-001:w:rPrChange`。
7. 尝试把 QA 输出写到工程 `build/` 外：以退出码 1 拒绝。

恶意与变异测试文件仅位于 `build/e0-fixture-audit/adversarial/`，受 `.gitignore` 排除。

## 当时列出的发布前动作（历史）

1. **已由后续记录闭环**：项目与 fixture 采用 Apache-2.0，`provenance.json` 已更新。
2. **已由后续记录闭环**：仓库内 clean-room 生成器替换了 Documents Skill 依赖。
3. **已由后续记录闭环**：`pyproject.toml`、`uv.lock` 和两次确定性重建提供环境证据。
4. **以最新门禁为准**：在 Word 或 LibreOffice 中完成视觉验收并保存可复核报告。
5. **持续要求**：公开发布前重跑当前 QA，只有最新门禁明确允许时才发布 fixture。
