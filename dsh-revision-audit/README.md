# dsh-revision-audit

对比新旧两份 LaTeX 稿，按语义块报告改动；给一份「直改清单」时，逐块判「已申报 / 未申报 / 申报未改」。

本地运行，不联网。

## 快速开始

1. 安装：桌面端 **插件 → 添加插件** 选择本目录，或 `dsh plugin add dsh-revision-audit`。
2. 对 Agent 说：

   > 用 revision_audit 对比 `paper/old.tex` 和 `paper/new.tex`，申报单在 `paper/直改清单.md`，报告写到 `tmp/audit.md`。

3. 看结果：`verdict`（pass / fail / unknown）、改动块数、每块结论、报告路径。

## 需要什么

- DSH 0.2.2-rc.3 或更新。
- Python 3.10+：优先使用 DSH 捆绑运行时；没有则在插件配置里给 `pythonPath`。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `old` | 是 | 旧稿 `.tex` 绝对路径 |
| `new` | 是 | 新稿 `.tex` 绝对路径 |
| `declaration` | 否 | 申报单 Markdown 表格；不给则只报客观块 |
| `report` | 否 | Markdown 明细输出路径 |

## 结论怎么读

- 有申报单且每块都能落到条目：`pass`。
- 有块落不到任何条目：`fail`（未申报）。
- 申报单里有文件级、无锚点的条目：落不到的块只能判 `unknown`，不硬判。

## 排查

- `只支持 LaTeX 源文件（.tex / .ltx）`：扩展名不对，先确认文件。
- `verdict: unknown` 且没给申报单：正常，工具只报客观改动块。
- `找不到文件`：两个路径都要绝对路径。
- `找不到 tex_change_audit.py`：包内 `engine/vendor/` 缺失，重新安装本包。

## 许可

MIT。
