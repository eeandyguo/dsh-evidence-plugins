# 改稿对账

按语义块对比新旧 LaTeX 稿；给申报单时逐块判「已申报 / 未申报 / 申报未改」。

## 用法

`revision_audit` 工具：传 `old`、`new` 两个 `.tex` 路径，可选 `declaration` 申报单。

## 边界

只支持 `.tex` / `.ltx`；不给申报单时只报客观改动块。

渲染与安全说明见包内 `README.md`。
