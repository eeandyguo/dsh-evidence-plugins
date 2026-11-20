# dsh-backup-verify

把一份备份真还原到临时目录，逐文件比对 sha256，再抽样打开 Office / sqlite / zip 文件。

它证明的是「这份备份能完整还原」，不是「它等于原来的机器」。

本地运行，不联网。

## 快速开始

1. 安装：桌面端 **插件 → 添加插件** 选择本目录，或 `dsh plugin add dsh-backup-verify`。
2. 对 Agent 说：

   > 用 backup_verify 验证 `D:\backup\2026-11.zip`，报告写到 `tmp/backup.md`。

3. 看结果：`verdict`、文件数、哈希不符数、缺失/多出、抽样打开数。

## 需要什么

- DSH 0.2.2-rc.3 或更新。
- Python 3.10+：优先使用 DSH 捆绑运行时；没有则在插件配置里给 `pythonPath`。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `source` | 是 | 备份的 `.zip` 文件或目录，绝对路径 |
| `report` | 否 | Markdown 报告路径 |

## 结论怎么读

- `pass`：还原后逐文件哈希一致，抽样打开无失败。
- `fail`：哈希不符、缺失、多出、抽样打不开，或备份里没有文件。
- `unknown`：源既不是目录也不是 zip，或超出文件数/字节上限。

Office 的 `~$` 锁文件、`.~lock.*`、`Thumbs.db` 等临时文件保留哈希但对账，不参与抽样打开。

## 排查

- `EMPTY_BACKUP`：备份里没有任何文件。
- `CORRUPT_CONTAINER`：扩展名是压缩包但内容不是有效 zip，备份可能已损坏。
- `UNSUPPORTED_SOURCE`：源既不是目录也不是 zip。
- `TEMP_NOT_REMOVED`：临时还原目录清理失败，报告里会给出路径，可手工删除。
- 文件数或字节超上限：默认 20000 个文件 / 4 GiB，超出会报 `LIMIT_EXCEEDED`。

## 许可

MIT。
