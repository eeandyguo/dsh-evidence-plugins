# dsh-stats-test

对一份 CSV 跑统计检验，输出统计量、p 值、效应量与前提检查。支持 t 检验、方差分析、卡方、Mann-Whitney、Kruskal-Wallis。

本地运行，不联网；p 值在本地计算，不依赖 scipy。

## 快速开始

1. 安装：桌面端 **插件 → 添加插件** 选择本目录，或 `dsh plugin add dsh-stats-test`。
2. 准备 CSV，至少两列，例如：

   ```csv
   group,value
   A,5.1
   A,4.9
   B,5.4
   B,5.6
   ```

3. 对 Agent 说：

   > 用 stats_test 跑 `tmp/data.csv` 的 t 检验，`group` 列分组，`value` 列数值。

4. 看结果：`verdict`、统计量、p 值、效应量、前提检查。

## 需要什么

- DSH 0.2.2-rc.3 或更新。
- Python 3.10+：优先使用 DSH 捆绑运行时；没有则在插件配置里给 `pythonPath`。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `data` | 是 | CSV 绝对路径，utf-8 或 gb18030 |
| `test` | 是 | `ttest` / `anova` / `chi2` / `mannwhitney` / `kruskal` |
| `group` | 是 | 分组列名 |
| `value` | 除卡方外必填 | 数值列名 |
| `group2` | 卡方必填 | 第二个分类列 |
| `report` | 否 | Markdown 报告路径 |

## 结论怎么读

`verdict` 表示**前提是否满足**，不是结果是否显著：

- 组样本量 < 5 会给 `SMALL_GROUP` 警告；
- 出现非数字行会剔除并给 `NON_NUMERIC_DROPPED`；
- 前提不全时 `verdict: unknown`，p 值不要直接当结论。

**本工具不做正态性检验**，小样本或明显偏态时请自行确认分布假设。

## 排查

- `spawn python.exe ENOENT`：没找到 Python，配 `pythonPath`。
- `NOT_ENOUGH_GROUPS`：t 检验要恰好 2 组；多组改用 `anova`。
- 卡方报缺列：`group2` 必填。
- 中文 CSV 乱码：确认编码是 utf-8 或 gb18030。

## 许可

MIT。
