# 资料与既有接口约定

物理身份为(snapshot_id,file_id,rowgroup,ordinal)，ordinal 基于删除前行序。列目录使用字段ID，各列块的[start,end)覆盖独立，可与其他列不对齐；删除位置不参与过滤结果。缺省值由该快照 schema 定义。columns.py 的 read_rows 是现有整行路径，返回物理身份；load_column 验证单列块并按物理行号取值。输入表达式只包含已登记字段上的比较、AND、OR、NOT及IS NULL。

所有资料均为合成，字段、身份及状态按本文解释。源码中的 inspect 入口只读取并核对现有基础接口；不会建立高级查询或仓库管理流程。

## 按需列块读取（warehouse/reader.py）

`open_reader(store, head)` 打开一个 rowgroup 清单；清单在打开时校验列块连续覆盖、摘要身份与删除范围，任何损坏都在读取前报错。

- **选择向量**（`Selection`）是同一 rowgroup 内的有序物理行引用，保存
  `(snapshot, file_id, rowgroup, ordinal)` 四元组，保留顺序和重复引用；
  它不是行号集合，`take()` 只截取前缀。
- **读取顺序**：过滤与排序所需列先读，选出 `Selection` 后再按投影取输出列。
  未出现在过滤、排序或输出中的列不会被读取。
- **物理对齐**：各列列块边界互不对齐，取值总是以同一 ordinal 回落到对应
  列块（`bisect` 定位），不会把一列的下标套到另一列。
- **缺省列**：旧文件清单中没有列块的字段由 `defaults` 提供，不产生读取。
- **三值逻辑**：`evaluate` 返回 `True`/`False`/`None`（UNKNOWN）；只有 TRUE
  进入选择向量。NULL 排序一律排在最后。
- **调度**：`strategy='batch'` 按批向量读取，`'intervals'` 把相邻 ordinal 合并
  成稀疏区间；两者结果一致，只影响读取方式。
- **预算**：`Budget(max_blocks=…, max_bytes=…)` 在每次真正读取前列块前检查，
  超限抛 `BudgetExceeded`，不会先读完整表再报错。`max_bytes` 只计列块字节，
  清单固定开销单独记为 `metadata_bytes`。
- **提前关闭**：`query()`/`fetch()` 惰性分批读取；`close()` 或上下文退出会
  停止生成器并清空列块缓存，不留下临时资源。未排序的 `LIMIT` 会让过滤在
  够数时停止，不读取后续列块。
- **失败优先**：跨快照/rowgroup 的选择向量抛 `SnapshotMismatch`；列块长度、
  摘要或行数损坏抛 `ValueError`；不会静默取错行。
- **可观察性**：`ReaderStats.report()` 返回 `blocks_read`、`bytes_read`、
  `metadata_bytes`、`fields_read`、`rows_examined/selected/output` 及逐块
  `column_blocks` 明细。`compare()` 用既有 `read_rows` 整行路径复核结果，并
  给出两边的实际读取字节。

```sh
python -m warehouse --compare --where '["=","20","a"]' --fields 10
python -m warehouse --selection 5,0,5,2 --fields 10,30
```
