# Office 版式质检

对 docx/pptx 只报客观项：空白页、图片有效分辨率、字体替换、自动缩字、图表引用不一致。

## 用法

`office_lint` 工具：传 `file`（docx/pptx）与可选 `report`。

## 边界

只报客观项，不判美观。docx 分页在 Word COM 下与 Word 一致；回退 LibreOffice 时页数仅参考。

渲染与安全说明见包内 `README.md`。
