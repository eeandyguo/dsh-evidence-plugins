# 统计检验

对 CSV 跑 t 检验、方差分析、卡方、Mann-Whitney 或 Kruskal-Wallis，输出统计量、p 值、效应量与前提检查。

## 用法

`stats_test` 工具：传 `data`（CSV）、`test`、`group`、`value`；卡方再传 `group2`。

## 边界

不做正态性检验；p 值本地计算，不依赖 scipy。

渲染与安全说明见包内 `README.md`。
