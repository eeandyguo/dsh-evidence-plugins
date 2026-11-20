# 备份验证

把 zip 或目录真还原到临时目录，逐文件比对 sha256，抽样打开 Office 与 sqlite 文件。

## 用法

`backup_verify` 工具：传 `source`（zip 或目录）与可选 `report`。

## 边界

证明备份能完整还原，不证明它等于原始机器数据；Office 锁文件不参与抽样。

渲染与安全说明见包内 `README.md`。
