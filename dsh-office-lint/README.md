# dsh-office-lint

对 `.docx` / `.pptx` 做客观版式检查：空白页、图片有效分辨率、字体替换、PowerPoint 自动缩字、图表引用与数据行数不一致。

只报客观项，不判美观。

本地运行，不联网。

## 快速开始

1. 安装：桌面端 **插件 → 添加插件** 选择本目录，或 `dsh plugin add dsh-office-lint`。
2. 对 Agent 说：

   > 用 office_lint 检查 `reports/申报书-v3.docx`，报告写到 `tmp/lint.md`。

3. 看结果：`verdict`、页数、空白页、低分辨率图片、缺失字体、渲染器。

## 需要什么

- DSH 0.2.2-rc.3 或更新，且当前 profile 有 Host Office kit（桌面端默认有）。
- Python 3.10+：优先使用 DSH 捆绑运行时；没有则在插件配置里给 `pythonPath`。
- Windows 且有 Microsoft Word 时，会优先用 Word COM 渲染，分页与 Word 一致；没有 Word 或 Word 正在运行则回退 LibreOffice。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | `.docx` 或 `.pptx` 绝对路径 |
| `report` | 否 | Markdown 报告路径 |

## 渲染器与页数

报告里的 `renderer` 会写 `word-com` 或 `libreoffice`。

- `word-com`：页数与 Word 状态栏一致，空白页可判 `fail`。
- `libreoffice`：分页可能与 Word 不同；页数不等时，空白页只报「未验证」，不判 `fail`。

配置里可设 `renderer: libreoffice`，完全不启动 PowerShell 与 Word——对杀毒软件敏感时用这个。

## 排查

- `找不到文件`：路径要绝对路径。
- `officeKit 未挂载`：当前 profile 没有 Office 渲染服务，换桌面端 profile。
- `Word 已在运行，跳过 COM`：Word 开着时不会抢会话；关掉 Word 再跑，或直接用 LibreOffice 渲染。
- `RENDER_FAILED` + `CORRUPT_CONTAINER`：文件本身不是有效的 OOXML 包，先确认文件没坏。
- 低分辨率阈值是 100 DPI；打印场景建议按 150 DPI 自查。

## 安全边界

- 不联网；Word 以只读方式打开源文档，不修改、不保存。
- 只有 `renderer=auto` 且本机有 Word 时才启动 Windows PowerShell 与 Word。
- 导出的临时 PDF 在系统临时目录，结束后清理。

## 许可

MIT。
