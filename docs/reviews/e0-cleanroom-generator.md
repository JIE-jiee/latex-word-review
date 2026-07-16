# E0 public fixture clean-room generator review

- 日期：2026-07-16
- 范围：`scripts/build_e0_public_fixture.py` 与
  `tests/fixtures/e0-minimal-paper/` 的可重建二进制资产
- 结论：生成器已不依赖仓库外 Skill 或本机路径；结构、静态 oracle、隐私、
  完整性及两次重建确定性均通过。逐页视觉验收仍独立阻断，不虚报通过。

## Clean-room 边界

实现前完整阅读了工程约束、任务说明、fixture README/provenance/expected
JSON、原生成器、增强 QA、只读 observer 与既有独立审计。实现期间未读取、
复制或导入 `$CODEX_HOME` 下的 Documents Skill 源码，也未访问私人论文目录。
旧 DOCX 只作为公开 fixture 的行为契约：静态 oracle 定义预期语义，解压后的
OPC member 用于最终等价性比对；没有从受限 Documents Skill 读取或复制实现。

## 复用与自研决策

| 能力 | 本机核验版本与许可证元数据 | 维护/接口证据 | 决策 | 尚存差距 |
|---|---|---|---|---|
| [python-docx](https://github.com/python-openxml/python-docx) | 1.2.0；MIT | 安装包元数据提供仓库、文档和 changelog；仓库 `fixture` 依赖组声明 `>=1.2,<2` | adopt，用于基础 Word 文档、样式、图片和表格对象 | 公共 API 不覆盖本 fixture 所需的完整 move、run-format revision 与底层批注 plumbing |
| [lxml](https://github.com/lxml/lxml) | 6.1.1；BSD-3-Clause | 安装包元数据提供源码与问题跟踪入口；核心依赖声明 `>=5.0` | adopt，用于禁用实体/网络的 XML 解析与标准 OOXML 编辑 | 不提供 Word 审阅语义，须由静态 oracle 约束 |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0；MIT-CMU | 安装包元数据提供源码、发布记录和文档；仓库 `fixture` 依赖组声明 `>=12,<13` | adopt，仅生成完全合成的 PNG | 页面渲染不在其能力范围内 |
| OPC/WordprocessingML glue | 无第三方代码；使用公开标准元素与关系类型 | 既有 `docs/reviews/e0-fixture-audit.md` 已确认外部 Skill 不可作为可分发依赖，增强 observer/QA 提供独立验证 | self-build，严格限于 fixture 生成；不进入生产 ingest | 仍需 Word/LibreOffice 做逐页视觉验收 |

本次 self-build 只包含可审计的小型 fixture 组装逻辑：固定表格 DXA 几何、
移除模板 custom XML/缩略图/rsid 属性、启用 `w:trackRevisions`、生成固定
`w:ins`/`w:del`/`w:moveFrom`/`w:moveTo`/`w:rPrChange`、批注 part、关系和
content-type override。它不替代 `tex2word`，也不是生产修订解析器。

## 实现结果

生成器现在：

1. 仅接受工程根与 fixture 根，fixture 必须位于 `tests/fixtures/`，临时输出
   固定在 `build/e0-cleanroom/work/`。
2. 使用固定作者、带时区时间、revision/comment/range ID 和 ZIP member 顺序；
   ZIP 时间戳固定为 1980-01-01。
3. 从清洁 base 的内存 OPC 副本派生 returned，不反向修改 LaTeX，也不读取
   已存在 returned 内容。
4. 保留七个书签、原生 OMML、合成图片、固定表格、页脚域、插入、删除、
   替换、移动、格式修订、范围批注和点批注。
5. 把无时间依赖的构建摘要写入 `build/e0-cleanroom/build-summary.json`。

根 `LICENSE` 已确认为 Apache License 2.0。fixture provenance 已使用 SPDX
`Apache-2.0`，公开再分发状态改为
`ready_for_public_redistribution_under_apache_2_0`；生成器字段明确记录
`external_helpers: []`。视觉状态继续单列为 `blocked_missing_libreoffice`。

## 验证证据

- `python -m py_compile scripts/build_e0_public_fixture.py
  scripts/qa_e0_public_fixture.py scripts/e0_inspect_docx.py`：通过。
- `ruff check scripts/build_e0_public_fixture.py`：通过。
- 连续运行两次 `python scripts/build_e0_public_fixture.py`：三个生成物哈希逐项
  相同，详见 `build/e0-cleanroom/determinism-report.json`。
- base SHA-256：
  `27d2fd5e4442485bb312cd413c5bb785121bd95a4b4813d5ecf3b80c58c58ed6`。
- returned SHA-256：
  `d1d71a408f817daf33c34a26a23d624bab04d2b02b4e2085a6a9cd44283a44c5`。
- PNG SHA-256：
  `4876c1459df27682e521577574a3151aae2961bfbce342153c2af26ca25fab15`。
- 增强 QA：structure、ChangeSet oracle、privacy、fixture integrity 均为
  `pass`；许可证、公开状态、确定性和仓库自包含生成器检查均为 `pass`。
  总 release readiness 仅因 `visual_review_complete` 为 `blocked`。
- `build/e0-cleanroom/clean-clone-smoke-locked-v2/`：复制公开仓库最小文件后
  删除 base/returned/PNG，以 `uv run --locked --group fixture` 从空缺状态重建；
  三个哈希与主 fixture 完全一致，隔离副本 QA 的 fixture integrity 为
  `pass`。主环境 CPython 3.12.13 与隔离副本 CPython 3.12.11 结果一致。
- clean-room 前后 base 和 returned 的 DOCX 容器哈希及全部解压 OPC member
  字节均逐项一致；OOXML、媒体、审阅语义和压缩容器均无漂移。

## 剩余门槛

需要在可用的 Word 或 LibreOffice 环境中渲染并人工核对分页、OMML、图片、
表格、修订气泡和批注锚点。完成前 `visual_review.status` 与 QA 总发布门禁保持
blocked；这不影响 Apache-2.0 再分发许可、生成器自包含性或 fixture 完整性。
