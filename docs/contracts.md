# 资料与既有接口约定

物理身份为(snapshot_id,file_id,rowgroup,ordinal)，ordinal 基于删除前行序。列目录使用字段ID，各列块的[start,end)覆盖独立，可与其他列不对齐；删除位置不参与过滤结果。缺省值由该快照 schema 定义。columns.py 的 read_rows 是现有整行路径，返回物理身份；load_column 验证单列块并按物理行号取值。输入表达式只包含已登记字段上的比较、AND、OR、NOT及IS NULL。

所有资料均为合成，字段、身份及状态按本文解释。源码中的 inspect 入口只读取并核对现有基础接口；不会建立高级查询或仓库管理流程。
