# 依赖与供应链审计（2026-07-16）

## 结论

截至 **2026-07-16 20:28:50 JST**，当前 `uv.lock` 的默认运行时闭包与“全部可选
extra + 全部 dependency group”闭包，分别经 `pip-audit 2.10.1` 的 OSV 和 PyPI
服务检查，四项结果均为 **0 个已知漏洞**。本结论是有时间点和范围限制的发布候选快照，
不是对未来漏洞、未披露漏洞、原生库或完整构建供应链的永久保证。

审计使用冻结锁文件导出，保留包哈希，且通过 `--no-emit-project` 排除本项目自身，因而本页
报告的是第三方 Python 依赖风险，不替代项目源码安全审查。原始 JSON、冻结 TXT 和摘要保存在
本地忽略目录 `build/final-dependency-audit-v3/`，不作为应提交的发布资产。

## 历史发现与修复

| 组件 | 初始状态与风险 | 修复 | 当前候选 |
|---|---|---|---|
| `lxml` | 原下界 `>=5.0` 可解析到 5.0.0；[PYSEC-2026-87 / CVE-2026-41066 / GHSA-vfmq-68hx-4jfw](https://osv.dev/vulnerability/PYSEC-2026-87) 涉及默认 `iterparse()` / `ETCompatXMLParser()` 的本地文件 XXE，修复版本为 6.1.0 | 将公开约束提高为 `>=6.1,<7` 并重锁 | `6.1.1` |
| `setuptools` | 开发/构建闭包曾锁定 80.9.0；[PYSEC-2026-3447 / CVE-2026-59890 / GHSA-h35f-9h28-mq5c](https://osv.dev/vulnerability/PYSEC-2026-3447) 指出 macOS APFS/HFS+ 上 Unicode 规范化差异可绕过 `MANIFEST.in` 排除规则，使本应排除的文件进入 sdist | 按上游修复版本精确固定为 `83.0.0` 并重锁 | `83.0.0` |
| `wheel` | 开发/构建闭包曾锁定 0.45.1；[PYSEC-2026-2047 / CVE-2026-24049 / GHSA-8rrh-rw8j-w5fx](https://github.com/advisories/GHSA-8rrh-rw8j-w5fx) 涉及 `wheel unpack` 对未净化归档路径执行权限修改，修复版本为 0.46.2 | 精确固定为更新的 `0.47.0` 并重锁 | `0.47.0` |

Hatchling 1.31.0 也被精确固定并进入开发锁。正式构建使用锁定 builder 和
`python -m build --no-isolation`，避免为项目 wheel/sdist 临时解析一个未审计的 PEP 517
隔离构建环境。这是可复现性和供应链边界强化，不表示 Hatchling 曾命中上述漏洞。

## 最终漏洞扫描

| 锁定闭包 | 服务 | 被审计的发行包 | 已知漏洞记录 | 证据 |
|---|---:|---:|---:|---|
| 默认运行时 | OSV | 10 | 0 | `runtime-default-osv.json` |
| 默认运行时 | PyPI | 10 | 0 | `runtime-default-pypi.json` |
| 全部 extras + 全部 groups | OSV | 66 | 0 | `all-extras-all-groups-osv.json` |
| 全部 extras + 全部 groups | PyPI | 66 | 0 | `all-extras-all-groups-pypi.json` |

两种闭包来自同一个通过 `uv lock --check` 的锁文件。审计命令使用 `--no-deps` 和
`--disable-pip`：依赖解析由 `uv.lock` 和冻结导出完成，`pip-audit` 只查询导出中已列出的
精确版本，不在审计时重新解析或调用 pip。

实际执行命令如下（PowerShell）：

```powershell
uv lock --check

uv export --frozen --no-dev --no-emit-project --no-annotate --no-header `
  --output-file build/final-dependency-audit-v3/runtime-default.txt

uv export --frozen --all-extras --all-groups --no-emit-project --no-annotate --no-header `
  --output-file build/final-dependency-audit-v3/all-extras-all-groups.txt

$env:PYTHONUTF8 = '1'
$AUDIT_PY = 'build/dependency-audit/pip-audit-venv/Scripts/python.exe'
$OUT = 'build/final-dependency-audit-v3'

& $AUDIT_PY -m pip_audit -r "$OUT/runtime-default.txt" --no-deps --disable-pip `
  --vulnerability-service osv --format json --output "$OUT/runtime-default-osv.json" `
  --cache-dir "$OUT/cache"
& $AUDIT_PY -m pip_audit -r "$OUT/runtime-default.txt" --no-deps --disable-pip `
  --vulnerability-service pypi --format json --output "$OUT/runtime-default-pypi.json" `
  --cache-dir "$OUT/cache"
& $AUDIT_PY -m pip_audit -r "$OUT/all-extras-all-groups.txt" --no-deps --disable-pip `
  --vulnerability-service osv --format json --output "$OUT/all-extras-all-groups-osv.json" `
  --cache-dir "$OUT/cache"
& $AUDIT_PY -m pip_audit -r "$OUT/all-extras-all-groups.txt" --no-deps --disable-pip `
  --vulnerability-service pypi --format json --output "$OUT/all-extras-all-groups-pypi.json" `
  --cache-dir "$OUT/cache"
```

## 直接依赖闭包

对 `src/latex_word_review/**/*.py` 做 AST import 清点，并另外检查动态导入后，生产代码直接
使用的第三方顶层模块是 `jsonschema`、`lxml`、`referencing`、`rfc8785` 和
`tex2word`。前四项是静态 import；`tex2word` 由受限 worker 通过
`importlib.import_module("tex2word")` 动态加载。五项均已在 `[project.dependencies]`
显式声明，没有发现依赖某个传递包“顺带安装”的未声明第三方 import。

`pylatexenc` 等默认闭包成员是 `tex2word` 或 JSON Schema 栈的传递依赖，并非本项目源码的
直接 import。Pandoc、TeX、LibreOffice、Microsoft Word 等通过单独安装的可执行程序边界
发现和调用，不属于 Python 运行时依赖闭包。

## 许可证检查

为避免只检查开发环境，审计把 `all-extras-all-groups.txt` 以 `--require-hashes` 同步到全新
Python 3.12 隔离环境，再读取全部 66 个已安装发行包的 `License-Expression`、`License`
和许可证 classifier：**66 个均有可识别许可证元数据，未知项为 0**。

直接运行时依赖的许可证为：

| 组件 | 锁定版本 | 上游许可证元数据 |
|---|---:|---|
| `jsonschema` | 4.26.0 | MIT |
| `lxml` | 6.1.1 | BSD-3-Clause |
| `referencing` | 0.37.0 | MIT |
| `rfc8785` | 0.1.4 | Apache-2.0 classifier |
| `tex2word` | 1.0.5 | MIT |

默认运行时的其余传递包报告 MIT、PSF-2.0 等兼容元数据。全量闭包中没有发现“无许可证
元数据”的包，但这不等同于对每一行上游源码做法律审计：例如 `docutils` 元数据同时列出
Public Domain、BSD 和 GPL classifier，它只位于发布/开发工具链；`pypdfium2` 元数据明确
列出 BSD-3-Clause、Apache-2.0 和 dependency licenses，并涉及 PDFium 原生组件。它们均由
包管理器单独安装，不会被复制进本项目 wheel/sdist；若未来发布容器、冻结可执行文件或
vendor 依赖，必须按实际再分发内容重新生成 notices 并做逐文件审查。

根 `THIRD_PARTY_NOTICES.md` 已覆盖五个直接运行时依赖、公共 fixture 的模板来源、外部
可执行程序边界和开发工具边界。当前项目 wheel/sdist 不内嵌这些 Python 依赖，因此没有因
本轮锁版本变化新增第三方二进制到发行包。

## 明确的覆盖边界

- 结果仅反映 2026-07-16 20:28:17–20:28:50 JST 查询时 OSV/PyPI 已收录的记录；服务可能
  延迟、纠正或采用不同别名，0 不代表不存在未知、未披露或尚未入库的漏洞。
- `pip-audit` 按 Python 发行包名称和版本查询，不深审 wheel 内或其链接的原生库。尤其是
  `lxml` 的 libxml2/libxslt 边界，以及可选 `pypdfium2` 的 PDFium 原生组件，需要各自的
  原生组件清单和漏洞数据源才能获得完整覆盖。
- 本轮没有证明包维护者身份、注册表账户安全、上游源码无恶意逻辑或构建机无入侵；冻结
  哈希可检测下载字节漂移，但不能把恶意且已锁定的字节变成可信字节。
- Windows 真 TeX CI 配置固定 MiKTeX 官方 Setup Utility
  `miktexsetup-5.5.0+1763023-x64.zip` 及官方 SHA-256，先无交互安装 basic 集合，再显式
  安装/验证版本库清单中的 29 包 E0 闭包，并解析 CTeX/Fandol 资源。v3
  `toolchain.json` 绑定清单摘要、必需包 digest、完整已安装包清单的计数/摘要、资源与工具
  版本；测试前后完整清单必须逐项一致。该证据仍以精确提交的 CI 结果为准；当前
  `uv.lock` 不覆盖 MiKTeX 包仓库和工具链。
- 发布证据中的 CycloneDX SBOM 只枚举从已安装项目 wheel 可达的、锁定的 Python 运行时
  闭包。它不包含 Hatchling 等 build/dev 工具、GitHub Actions、Windows runner 镜像、
  MiKTeX Setup Utility、MiKTeX 包或外部 Word/Pandoc 工具，不能表述为“完整供应链 SBOM”。
- 对仅提供源码归档的运行时依赖（当前某些平台上的 `pylatexenc`），clean-install 会先校验
  锁文件中的下载哈希，再用已锁定 builder 禁用隔离构建并对本地 wheel 重新哈希；但当前
  不会把两次独立 clean-install 生成的该依赖 wheel 做逐字节比较。因此，本项目自身的
  wheel/sdist 可复现证明不能外推为所有传递依赖构建产物都已证明可复现。
- `provenance.intoto.json` 是仓库自定义、未签名、使用 SLSA v1 predicate URI 的兼容记录；
  它不是签名 attestation，也不证明 runner 身份或达到某一 SLSA 构建等级。

因此，本轮可支持的准确结论是：**当前冻结 Python 依赖快照未被两项服务检出已知漏洞，
直接 import 与声明闭包一致，现有单独安装模型未发现许可证阻断项**。任何新锁文件、发布
形态或审计日期都应重新执行同一流程。
