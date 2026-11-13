"""按需列块读取、三值过滤、选择向量及可观测预算。

这层只依赖 :class:`warehouse.objects.ObjectStore` 与 ``columns.py`` 写出的
rowgroup 清单，不改变既有 ``read_rows`` / ``load_column`` 的契约。

约定：

* 物理身份为 ``(snapshot_id, file_id, rowgroup, ordinal)``，ordinal 基于
  删除前行序。选择向量保存完整的四元组，而不是把 ordinal 当成新行号。
* 选择向量是**有序序列**，允许重复引用；本模块从不用集合或行号去重。
* 各列列块的 ``[start, end)`` 可以彼此不对齐，取值一律以同一物理
  ordinal 回落到对应列块。
* 过滤表达式使用三值逻辑（TRUE / FALSE / UNKNOWN），只有 TRUE 保留。
* 列块按需读取；预算在真正读取前检查，绝不先物化整表再报告超限。
"""

from bisect import bisect_right
import json

from .typed import typed


class SnapshotMismatch(ValueError):
    """选择向量属于另一个快照或 rowgroup。"""


class BudgetExceeded(ValueError):
    """按需读取会越过调用者给定的预算。"""


class _Desc:
    """用于混合方向排序的值包装；NULL 由外层标记统一排在最后。"""

    __slots__ = ('value',)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        if not isinstance(other, _Desc):
            return NotImplemented
        return other.value < self.value

    def __eq__(self, other):
        if not isinstance(other, _Desc):
            return NotImplemented
        return typed(self.value) == typed(other.value)

    def __hash__(self):
        return hash((_Desc, typed(self.value)))


# ---------------------------------------------------------------------------
# 三值表达式
# ---------------------------------------------------------------------------

_COMPARE = {
    '=': '=', '==': '=', 'eq': '=',
    '!=': '!=', '<>': '!=', 'ne': '!=',
    '<': '<', 'lt': '<', '<=': '<=', 'lte': '<=',
    '>': '>', 'gt': '>', '>=': '>=', 'gte': '>=',
}

_LOGICAL = ('and', 'or')
_NODE_TAGS = ('cmp', 'null', 'and', 'or', 'not')


def _unpack(expr):
    if isinstance(expr, dict):
        return expr
    if isinstance(expr, (list, tuple)) and expr:
        head = expr[0]
        if not isinstance(head, str):
            raise ValueError('过滤表达式格式非法')
        return {'op': head, 'args': list(expr[1:])}
    raise ValueError('过滤表达式格式非法')


def _parse(expr):
    node = _unpack(expr)
    op = node.get('op')
    if not isinstance(op, str):
        raise ValueError('过滤表达式缺少 op')
    name = op.strip().lower()

    if name in _LOGICAL:
        args = node.get('args')
        if args is None:
            left, right = node.get('left'), node.get('right')
            if left is None or right is None:
                raise ValueError('逻辑表达式缺少参数')
            args = [left, right]
        if not isinstance(args, (list, tuple)) or not args:
            raise ValueError('逻辑表达式参数非法')
        allowed = ('cmp', 'null', 'and', 'or', 'not')
        return (name, tuple(arg if isinstance(arg, tuple) and arg and arg[0] in allowed
                            else _parse(arg) for arg in args))

    if name == 'not':
        args = node.get('args')
        arg = node.get('arg')
        if arg is None and isinstance(args, (list, tuple)) and len(args) == 1:
            arg = args[0]
        if arg is None:
            raise ValueError('NOT 缺少参数')
        if isinstance(arg, tuple) and arg and arg[0] in _NODE_TAGS:
            return ('not', arg)
        return ('not', _parse(arg))

    if name in ('is null', 'isnull', 'is_null'):
        field = node.get('field')
        if field is None and isinstance(node.get('args'), (list, tuple)) and node['args']:
            field = node['args'][0]
        if not isinstance(field, str):
            raise ValueError('IS NULL 缺少字段')
        return ('null', field)

    if name in ('is not null', 'isnotnull', 'is_not_null'):
        field = node.get('field')
        if field is None and isinstance(node.get('args'), (list, tuple)) and node['args']:
            field = node['args'][0]
        if not isinstance(field, str):
            raise ValueError('IS NOT NULL 缺少字段')
        return ('not', ('null', field))

    if name not in _COMPARE:
        raise ValueError('不支持的过滤操作')
    field = node.get('field')
    value = node.get('value')
    if field is None and isinstance(node.get('args'), (list, tuple)):
        args = node['args']
        if len(args) == 2:
            field, value = args
    if not isinstance(field, str):
        raise ValueError('比较缺少字段')
    return ('cmp', _COMPARE[name], field, value)


def compile_expression(expr):
    """校验并归一化表达式，供逐行求值重用；已编译节点原样返回。"""
    if isinstance(expr, tuple) and expr and expr[0] in _NODE_TAGS:
        return expr
    return _parse(expr)


def expression_fields(expr):
    """返回表达式引用的字段，顺序为首次出现顺序。"""
    node = compile_expression(expr)
    found = []

    def visit(part):
        tag = part[0]
        if tag == 'cmp':
            found.append(part[2])
        elif tag == 'null':
            found.append(part[1])
        elif tag == 'not':
            visit(part[1])
        else:
            for child in part[1]:
                visit(child)

    visit(node)
    return tuple(dict.fromkeys(found))


def _compare(op, left, right):
    if left is None or right is None:
        return None  # UNKNOWN
    if op == '=':
        return typed(left) == typed(right)
    if op == '!=':
        return typed(left) != typed(right)
    if isinstance(left, bool) or isinstance(right, bool):
        raise ValueError('布尔值不能参与大小比较')
    if type(left) is not type(right):
        raise ValueError('比较字段类型不一致')
    if not isinstance(left, (int, str)):
        raise ValueError('比较字段类型不支持')
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    if op == '>':
        return left > right
    return left >= right


def _evaluate(node, values):
    tag = node[0]
    if tag == 'cmp':
        return _compare(node[1], values[node[2]], node[3])
    if tag == 'null':
        return values[node[1]] is None
    if tag == 'not':
        inner = _evaluate(node[1], values)
        return None if inner is None else not inner
    if tag == 'and':
        result = True
        for child in node[1]:
            value = _evaluate(child, values)
            if value is False:
                return False
            if value is None:
                result = None
        return result
    result = False  # or
    for child in node[1]:
        value = _evaluate(child, values)
        if value is True:
            return True
        if value is None:
            result = None
    return result


def evaluate(expr, values):
    """返回 ``True`` / ``False`` / ``None``（SQL 三值逻辑的 UNKNOWN）。"""
    return _evaluate(compile_expression(expr), values)


# ---------------------------------------------------------------------------
# 选择向量与读取统计
# ---------------------------------------------------------------------------


class Selection:
    """同一 rowgroup 内的有序物理行引用，保留顺序及重复。"""

    __slots__ = ('snapshot', 'file_id', 'rowgroup', 'count', '_ordinals',
                 'allow_deleted')

    def __init__(self, snapshot, file_id, rowgroup, count, ordinals,
                 allow_deleted=False):
        self.snapshot = snapshot
        self.file_id = file_id
        self.rowgroup = rowgroup
        self.count = count
        self._ordinals = tuple(ordinals)
        self.allow_deleted = allow_deleted

    @property
    def ordinals(self):
        return self._ordinals

    @property
    def identities(self):
        return tuple((self.snapshot, self.file_id, self.rowgroup, ordinal)
                     for ordinal in self._ordinals)

    def identity_at(self, index):
        return (self.snapshot, self.file_id, self.rowgroup, self._ordinals[index])

    def take(self, limit):
        if limit is None:
            return self
        return Selection(self.snapshot, self.file_id, self.rowgroup,
                         self.count, self._ordinals[:limit],
                         allow_deleted=self.allow_deleted)

    def __len__(self):
        return len(self._ordinals)

    def __iter__(self):
        return iter(self._ordinals)

    def __getitem__(self, index):
        return self._ordinals[index]

    def __eq__(self, other):
        return (isinstance(other, Selection) and
                (self.snapshot, self.file_id, self.rowgroup, self.count,
                 self._ordinals) ==
                (other.snapshot, other.file_id, other.rowgroup, other.count,
                 other._ordinals))

    def __repr__(self):
        shown = self._ordinals if len(self._ordinals) <= 12 else self._ordinals[:12] + ('…',)
        return (f'Selection(snapshot={self.snapshot[:12]}…, file_id={self.file_id!r}, '
                f'rowgroup={self.rowgroup!r}, ordinals={shown!r})')


class Budget:
    """列块读取预算；``None`` 表示该维度不设上限。

    ``max_bytes`` 只约束列块字节（清单是查询必需的固定开销，单独记为
    ``metadata_bytes``），``max_blocks`` 约束新读取的列块个数。
    """

    __slots__ = ('max_blocks', 'max_bytes')

    def __init__(self, max_blocks=None, max_bytes=None):
        for name, value in (('max_blocks', max_blocks), ('max_bytes', max_bytes)):
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, int) or value < 0):
                raise ValueError(f'{name} 必须是非负整数')
        self.max_blocks = max_blocks
        self.max_bytes = max_bytes

    def __repr__(self):
        return f'Budget(max_blocks={self.max_blocks}, max_bytes={self.max_bytes})'


class ReaderStats:
    """实际列块读取的可观察记录，用于比较优化前后的字节量。"""

    def __init__(self, budget=None, metadata_bytes=0):
        self.budget = budget
        self.metadata_bytes = metadata_bytes
        self.column_blocks = []
        self.block_cache_hits = 0
        self.bytes_read = 0
        self.rows_examined = 0
        self.rows_selected = 0
        self.rows_output = 0

    @property
    def blocks_read(self):
        return len(self.column_blocks)

    @property
    def fields_read(self):
        return tuple(sorted({entry['field'] for entry in self.column_blocks}))

    def report(self):
        return {
            'blocks_read': self.blocks_read,
            'block_cache_hits': self.block_cache_hits,
            'bytes_read': self.bytes_read,
            'metadata_bytes': self.metadata_bytes,
            'fields_read': list(self.fields_read),
            'rows_examined': self.rows_examined,
            'rows_selected': self.rows_selected,
            'rows_output': self.rows_output,
            'column_blocks': [dict(entry) for entry in self.column_blocks],
        }


# ---------------------------------------------------------------------------
# 读取器
# ---------------------------------------------------------------------------


def _normalise_sort(sort):
    if sort is None:
        return []
    if isinstance(sort, str):
        sort = [sort]
    if not isinstance(sort, (list, tuple)):
        raise ValueError('排序规格非法')
    keys = []
    for item in sort:
        if isinstance(item, str):
            keys.append((item, 'asc'))
            continue
        if isinstance(item, (list, tuple)) and len(item) == 2:
            field, direction = item
        elif isinstance(item, dict):
            field, direction = item.get('field'), item.get('direction', 'asc')
        else:
            raise ValueError('排序规格非法')
        if not isinstance(field, str):
            raise ValueError('排序字段非法')
        if not isinstance(direction, str) or direction.lower() not in ('asc', 'desc'):
            raise ValueError('排序方向非法')
        keys.append((field, direction.lower()))
    return keys


def _sort_key_factory(keys, columns):
    def sort_key(position):
        parts = []
        for (field, direction), column in zip(keys, columns):
            value = column[position]
            if value is None:
                parts.append((1, 0))  # NULL 最后
            elif direction == 'desc':
                parts.append((0, _Desc(value)))
            else:
                parts.append((0, value))
        return tuple(parts)

    return sort_key


class ColumnReader:
    """打开一个 rowgroup 清单，按需读取列块。"""

    def __init__(self, store, head, *, budget=None):
        self.store = store
        self.snapshot = head
        self.budget = budget
        self._cache = {}
        self._closed = False
        self.last_stats = None

        try:
            metadata_bytes = store.size(head)
        except ValueError:
            raise ValueError('rowgroup 清单不存在') from None
        try:
            manifest = store.json(head)
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError('rowgroup 清单损坏') from error
        if not isinstance(manifest, dict):
            raise ValueError('rowgroup 清单格式非法')
        self.metadata_bytes = metadata_bytes
        self.manifest = manifest
        self.count = self._validate_count(manifest.get('count'))
        self.file_id = manifest.get('file_id')
        self.rowgroup = manifest.get('rowgroup')
        if not isinstance(self.file_id, str) or not self.file_id:
            raise ValueError('rowgroup 缺少 file_id')
        if (isinstance(self.rowgroup, bool) or not isinstance(self.rowgroup, int)
                or self.rowgroup < 0):
            raise ValueError('rowgroup 编号非法')
        self.defaults = self._validate_defaults(manifest.get('defaults', {}))
        self.columns = self._validate_columns(manifest.get('columns', {}))
        self.deleted = self._validate_deleted(manifest.get('deleted', []))
        self.deleted_set = frozenset(self.deleted)
        self._starts = {
            field: [block['start'] for block in blocks]
            for field, blocks in self.columns.items()
        }
        self.registered_fields = tuple(
            list(self.columns) + [field for field in self.defaults
                                  if field not in self.columns]
        )

    # -- 清单校验 ---------------------------------------------------------

    @staticmethod
    def _validate_count(count):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError('rowgroup 行数非法')
        return count

    @staticmethod
    def _validate_defaults(defaults):
        if defaults is None:
            defaults = {}
        if not isinstance(defaults, dict):
            raise ValueError('缺省列非法')
        for field in defaults:
            if not isinstance(field, str):
                raise ValueError('缺省列字段非法')
        return dict(defaults)

    def _validate_columns(self, columns):
        if columns is None:
            columns = {}
        if not isinstance(columns, dict):
            raise ValueError('列目录非法')
        result = {}
        for field, blocks in columns.items():
            if not isinstance(field, str):
                raise ValueError('列字段非法')
            if not isinstance(blocks, (list, tuple)):
                raise ValueError('列块目录非法')
            checked = []
            for block in blocks:
                if not isinstance(block, dict):
                    raise ValueError('列块描述非法')
                start, end, digest = block.get('start'), block.get('end'), block.get('digest')
                if (isinstance(start, bool) or not isinstance(start, int) or
                        isinstance(end, bool) or not isinstance(end, int)):
                    raise ValueError('列块区间非法')
                if not 0 <= start < end <= self.count:
                    raise ValueError('列块区间非法')
                self.store.check_identity(digest)
                checked.append({'start': start, 'end': end, 'digest': digest})
            checked.sort(key=lambda item: item['start'])
            cursor = 0
            for block in checked:
                if block['start'] != cursor:
                    raise ValueError('列块未连续覆盖全部行')
                cursor = block['end']
            if cursor != self.count:
                raise ValueError('列块未覆盖全部行')
            result[field] = checked
        return result

    def _validate_deleted(self, deleted):
        if deleted is None:
            deleted = []
        if not isinstance(deleted, (list, tuple)):
            raise ValueError('删除位图非法')
        result = []
        for position in deleted:
            if (isinstance(position, bool) or not isinstance(position, int)
                    or position < 0 or position >= self.count):
                raise ValueError('删除位置非法')
            result.append(position)
        return tuple(result)

    # -- 生命周期 ---------------------------------------------------------

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError('读取器已关闭')

    def close(self):
        """丢弃列块缓存；幂等，且不留下任何临时资源。"""
        self._cache.clear()
        self._closed = True

    def clear_cache(self):
        self._cache.clear()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def new_stats(self, budget=None):
        return ReaderStats(budget=self.budget if budget is None else budget,
                           metadata_bytes=self.metadata_bytes)

    def _stats(self, stats):
        if stats is None:
            stats = self.new_stats()
        self.last_stats = stats
        return stats

    @property
    def stats(self):
        """最近一次操作的读取统计。"""
        return self.last_stats

    def blocks(self, field):
        """返回某字段的列块目录副本（start/end/digest），不读取数据。"""
        if field not in self.registered_fields:
            raise ValueError(f'字段不存在: {field}')
        return [dict(block) for block in self.columns.get(field, ())]

    def read_column(self, field, ordinals=None, *, strategy='intervals',
                    batch_size=1024, stats=None):
        """按物理 ordinal 顺序返回某列取值；缺省列不产生列块读取。"""
        self._ensure_open()
        if field not in self.registered_fields:
            raise ValueError(f'字段不存在: {field}')
        if ordinals is None:
            ordinals = self.live_ordinals()
        else:
            values = list(ordinals)
            ordinals = [self._check_ordinal(ordinal, allow_deleted=True)
                        for ordinal in values]
        stats = self._stats(stats)
        return self._values_for(field, ordinals, stats, strategy, batch_size)

    # -- 物理位置 ---------------------------------------------------------

    def live_ordinals(self):
        """删除位图之后的物理 ordinal，升序。"""
        return tuple(i for i in range(self.count) if i not in self.deleted_set)

    def _check_ordinal(self, ordinal, *, allow_deleted=False):
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ValueError('物理行号非法')
        if ordinal < 0 or ordinal >= self.count:
            raise ValueError('物理行号超出 rowgroup')
        if ordinal in self.deleted_set and not allow_deleted:
            raise ValueError('物理行号引用了已删除行')
        return ordinal

    def selection(self, ordinals=None, *, allow_deleted=False):
        """构造选择向量；``None`` 表示全部未删除行，顺序与重复均保留。"""
        self._ensure_open()
        if ordinals is None:
            ordinals = self.live_ordinals()
        else:
            if isinstance(ordinals, (str, bytes, dict)) or not hasattr(ordinals, '__iter__'):
                raise ValueError('选择向量必须是物理行号序列')
            values = list(ordinals)
            if any(isinstance(value, bool) or not isinstance(value, int)
                   for value in values):
                raise ValueError('选择向量必须只含整数行号')
            ordinals = tuple(values)
        checked = [self._check_ordinal(ordinal, allow_deleted=allow_deleted)
                   for ordinal in ordinals]
        return Selection(self.snapshot, self.file_id, self.rowgroup,
                         self.count, checked, allow_deleted=allow_deleted)

    def validate_selection(self, selection):
        """确认选择向量属于同一快照/rowgroup；不同物理身份明确失败。

        显式选择向量可以引用删除行（物理身份仍然存在，ordinal 基于删除
        前序）；``select`` 产出的选择向量永远不含删除行。
        """
        self._ensure_open()
        if not isinstance(selection, Selection):
            raise ValueError('需要选择向量')
        if selection.snapshot != self.snapshot:
            raise SnapshotMismatch(
                f'选择向量属于快照 {selection.snapshot}，读取器是 {self.snapshot}')
        if selection.file_id != self.file_id or selection.rowgroup != self.rowgroup:
            raise SnapshotMismatch('选择向量属于另一个 rowgroup')
        if selection.count != self.count:
            raise SnapshotMismatch('选择向量行数与 rowgroup 不一致')
        for ordinal in selection.ordinals:
            self._check_ordinal(ordinal,
                                allow_deleted=selection.allow_deleted)
        return selection

    def _block_index(self, field, ordinal):
        blocks = self.columns[field]
        index = bisect_right(self._starts[field], ordinal) - 1
        if index < 0 or ordinal >= blocks[index]['end']:
            raise ValueError('物理行号不在列块覆盖范围内')
        return index

    # -- 列块读取 ---------------------------------------------------------

    def _read_block(self, field, index, stats):
        key = (field, index)
        cached = self._cache.get(key)
        if cached is not None:
            stats.block_cache_hits += 1
            return cached

        block = self.columns[field][index]
        expected = self.store.size(block['digest'])
        budget = stats.budget
        if budget is not None:
            if (budget.max_blocks is not None and
                    stats.blocks_read + 1 > budget.max_blocks):
                raise BudgetExceeded(
                    f'读取 {field} 列块将超过 max_blocks={budget.max_blocks}')
            if (budget.max_bytes is not None and
                    stats.bytes_read + expected > budget.max_bytes):
                raise BudgetExceeded(
                    f'读取 {field} 列块将超过 max_bytes={budget.max_bytes}')

        raw = self.store.get(block['digest'])
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError('列块内容损坏') from error
        if not isinstance(chunk, list) or len(chunk) != block['end'] - block['start']:
            raise ValueError('列块行数损坏')

        self._cache[key] = chunk
        stats.bytes_read += len(raw)
        stats.column_blocks.append({
            'snapshot': self.snapshot,
            'file_id': self.file_id,
            'rowgroup': self.rowgroup,
            'field': field,
            'block': index,
            'start': block['start'],
            'end': block['end'],
            'digest': block['digest'],
            'bytes': len(raw),
        })
        return chunk

    def _ensure_blocks(self, field, ordinals, stats, strategy, batch_size):
        """只读取与所需物理行相交的列块。"""
        if field not in self.columns:
            return
        if strategy not in ('batch', 'intervals'):
            raise ValueError('调度策略非法')
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError('批大小必须是正整数')

        if strategy == 'batch':
            for offset in range(0, len(ordinals), batch_size):
                batch = ordinals[offset:offset + batch_size]
                for index in sorted({self._block_index(field, ordinal)
                                     for ordinal in batch}):
                    self._read_block(field, index, stats)
            return

        # 稀疏区间：把每个连续段映射到它覆盖的列块，绝不展开整列。
        unique = sorted(set(ordinals))
        if not unique:
            return
        indexes = set()
        start = previous = unique[0]
        for ordinal in unique[1:]:
            if ordinal == previous + 1:
                previous = ordinal
                continue
            indexes.update(range(self._block_index(field, start),
                                 self._block_index(field, previous) + 1))
            start = previous = ordinal
        indexes.update(range(self._block_index(field, start),
                             self._block_index(field, previous) + 1))
        for index in sorted(indexes):
            self._read_block(field, index, stats)

    def _values_for(self, field, ordinals, stats, strategy='batch', batch_size=1024):
        if field not in self.registered_fields:
            raise ValueError(f'字段不存在: {field}')
        ordinals = list(ordinals)
        if field not in self.columns:
            # 旧文件缺省列：由该 rowgroup 清单的 defaults 定义，不读列块。
            return [self.defaults[field]] * len(ordinals)
        self._ensure_blocks(field, ordinals, stats, strategy, batch_size)
        values = []
        for ordinal in ordinals:
            index = self._block_index(field, ordinal)
            block = self.columns[field][index]
            values.append(self._cache[(field, index)][ordinal - block['start']])
        return values

    # -- 调度 -------------------------------------------------------------

    @staticmethod
    def _schedule(items, strategy, batch_size):
        """把有序位置序列切成批次，供批向量或稀疏区间两种调度使用。"""
        if strategy not in ('batch', 'intervals'):
            raise ValueError('调度策略非法')
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError('批大小必须是正整数')
        if not items:
            return
        if strategy == 'batch':
            for offset in range(0, len(items), batch_size):
                yield list(items[offset:offset + batch_size])
            return
        start = previous = items[0]
        for item in items[1:]:
            if item == previous + 1:
                previous = item
                continue
            yield list(range(start, previous + 1))
            start = previous = item
        yield list(range(start, previous + 1))

    # -- 查询阶段 ---------------------------------------------------------

    def select(self, where=None, *, limit=None, strategy='batch',
               batch_size=1024, stats=None):
        """按三值逻辑过滤，返回有序 ``Selection``；只有 TRUE 保留。"""
        self._ensure_open()
        if limit is not None and (isinstance(limit, bool) or
                                  not isinstance(limit, int) or limit < 0):
            raise ValueError('LIMIT 必须是非负整数')
        stats = self._stats(stats)
        live = self.live_ordinals()
        if limit == 0:
            result = Selection(self.snapshot, self.file_id, self.rowgroup,
                               self.count, ())
            stats.rows_selected = 0
            return result
        if where is None:
            selected = live if limit is None else live[:limit]
            result = Selection(self.snapshot, self.file_id, self.rowgroup,
                               self.count, selected)
            stats.rows_selected = len(result)
            return result

        node = compile_expression(where)
        fields = expression_fields(node)
        for field in fields:
            if field not in self.registered_fields:
                raise ValueError(f'字段不存在: {field}')
        # LIMIT 时缩小批次，够数即停，不为一两行读取整批列块。
        step = batch_size
        if limit is not None:
            step = min(batch_size, limit)
        selected = []
        for batch in self._schedule(live, strategy, step):
            columns = {field: self._values_for(field, batch, stats, strategy,
                                               step)
                       for field in fields}
            for offset, ordinal in enumerate(batch):
                values = {field: column[offset]
                          for field, column in columns.items()}
                stats.rows_examined += 1
                if _evaluate(node, values) is True:
                    selected.append(ordinal)
                    if limit is not None and len(selected) >= limit:
                        result = Selection(self.snapshot, self.file_id,
                                           self.rowgroup, self.count, selected)
                        stats.rows_selected = len(result)
                        return result
        result = Selection(self.snapshot, self.file_id, self.rowgroup,
                           self.count, selected)
        stats.rows_selected = len(result)
        return result

    def sort(self, selection, sort=None, *, strategy='intervals',
             batch_size=1024, stats=None):
        """稳定排序，保持物理身份与重复引用；NULL 一律排在最后。"""
        self.validate_selection(selection)
        keys = _normalise_sort(sort)
        stats = self._stats(stats)
        if not keys:
            return selection
        for field, _ in keys:
            if field not in self.registered_fields:
                raise ValueError(f'字段不存在: {field}')
        ordinals = list(selection.ordinals)
        columns = [self._values_for(field, ordinals, stats, strategy, batch_size)
                   for field, _ in keys]
        try:
            order = sorted(range(len(ordinals)),
                           key=_sort_key_factory(keys, columns))
        except TypeError as error:
            raise ValueError('排序字段类型不一致') from error
        result = Selection(self.snapshot, self.file_id, self.rowgroup,
                           self.count, [ordinals[i] for i in order],
                           allow_deleted=selection.allow_deleted)
        return result

    def fetch(self, selection, fields=None, *, strategy='intervals',
              batch_size=1024, stats=None):
        """按选择向量顺序取值；重复引用产生重复输出行。

        生成器按调度分批读取，调用者提前 ``close()`` 时不会继续读取剩余
        列块，也不物化未用到的列。
        """
        self.validate_selection(selection)
        stats = self._stats(stats)
        fields = list(self.registered_fields) if fields is None else list(fields)
        for field in fields:
            if field not in self.registered_fields:
                raise ValueError(f'字段不存在: {field}')

        ordinals = list(selection.ordinals)
        for batch in self._schedule(ordinals, strategy, batch_size):
            columns = {field: self._values_for(field, batch, stats, strategy,
                                               batch_size)
                       for field in fields}
            for offset, ordinal in enumerate(batch):
                stats.rows_output += 1
                yield {
                    'identity': [self.snapshot, self.file_id,
                                 self.rowgroup, ordinal],
                    'values': {field: columns[field][offset] for field in fields},
                }

    # -- 组合入口 ---------------------------------------------------------

    def query(self, *, where=None, sort=None, fields=None, limit=None,
              strategy='batch', fetch_strategy=None, batch_size=1024,
              budget=None):
        """按需查询：先过滤/排序列，再按选择向量取输出列。"""
        self._ensure_open()
        return QueryResult(self, where=where, sort=sort, fields=fields,
                           limit=limit, strategy=strategy,
                           fetch_strategy=fetch_strategy,
                           batch_size=batch_size, budget=budget)


class QueryResult:
    """惰性查询结果，同时是上下文管理器；``stats`` 始终可观察。"""

    def __init__(self, reader, *, where, sort, fields, limit, strategy,
                 fetch_strategy, batch_size, budget):
        self.reader = reader
        self.where = where
        self.sort = sort
        self.fields = fields
        self.limit = limit
        self.strategy = strategy
        self.fetch_strategy = fetch_strategy or strategy
        self.batch_size = batch_size
        self.stats = reader.new_stats(budget)
        self._iterator = None
        self._closed = False

    def _run(self):
        stats = self.stats
        # 无排序时 LIMIT 能让过滤阶段提前停止读取后续列块。
        select_limit = None if self.sort is not None else self.limit
        selection = self.reader.select(
            self.where, limit=select_limit, strategy=self.strategy,
            batch_size=self.batch_size, stats=stats)
        if self.sort is not None:
            selection = self.reader.sort(
                selection, self.sort, strategy=self.fetch_strategy,
                batch_size=self.batch_size, stats=stats)
        if self.limit is not None:
            selection = selection.take(self.limit)
        stats.rows_selected = len(selection)
        yield from self.reader.fetch(
            selection, self.fields, strategy=self.fetch_strategy,
            batch_size=self.batch_size, stats=stats)

    def __iter__(self):
        if self._closed:
            raise RuntimeError('查询已关闭')
        if self._iterator is None:
            self._iterator = self._run()
        return self

    def __next__(self):
        if self._closed:
            raise RuntimeError('查询已关闭')
        if self._iterator is None:
            self._iterator = self._run()
        try:
            return next(self._iterator)
        except StopIteration:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        if self._iterator is not None:
            self._iterator.close()
        self._closed = True
        self.reader.clear_cache()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def report(self):
        return self.stats.report()


# ---------------------------------------------------------------------------
# 与慢速整行读取的对照
# ---------------------------------------------------------------------------


class _CountingStore:
    """只用于对照的字节计数包装，不改变底层对象。"""

    def __init__(self, store):
        self.store = store
        self.bytes_read = 0
        self.reads = 0

    def get(self, digest):
        raw = self.store.get(digest)
        self.bytes_read += len(raw)
        self.reads += 1
        return raw

    def json(self, digest):
        return json.loads(self.get(digest))

    def put(self, raw):
        return self.store.put(raw)

    def put_json(self, value):
        return self.store.put_json(value)

    def size(self, digest):
        return self.store.size(digest)

    def check_identity(self, digest):
        return self.store.check_identity(digest)


def _baseline_rows(store, head, where, sort, fields, limit):
    """既有慢速整行路径：清单全部列块 + 逐行，再过滤/排序/截断。"""
    manifest = store.json(head)
    count = manifest['count']
    defaults = manifest.get('defaults', {})
    live = set(range(count)) - set(manifest.get('deleted', []))
    columns = {}
    for field, blocks in manifest['columns'].items():
        values = []
        for block in blocks:
            chunk = store.json(block['digest'])
            if not isinstance(chunk, list) or len(chunk) != block['end'] - block['start']:
                raise ValueError('列块行数损坏')
            values.extend(chunk)
        if len(values) != count:
            raise ValueError('列块未覆盖全部行')
        columns[field] = values

    node = None if where is None else compile_expression(where)
    if node is not None:
        for field in expression_fields(node):
            if field not in columns and field not in defaults:
                raise ValueError(f'字段不存在: {field}')

    rows = []
    for ordinal in range(count):
        if ordinal not in live:
            continue
        values = {field: column[ordinal] for field, column in columns.items()}
        for field, value in defaults.items():
            values.setdefault(field, value)
        if node is not None and _evaluate(node, values) is not True:
            continue
        rows.append({'identity': [head, manifest['file_id'],
                                  manifest['rowgroup'], ordinal],
                     'values': values})

    keys = _normalise_sort(sort)
    if keys:
        for field, _ in keys:
            if field not in columns and field not in defaults:
                raise ValueError(f'字段不存在: {field}')
        values = [[row['values'][field] for row in rows] for field, _ in keys]
        try:
            order = sorted(range(len(rows)),
                           key=_sort_key_factory(keys, values))
        except TypeError as error:
            raise ValueError('排序字段类型不一致') from error
        rows = [rows[pos] for pos in order]

    if limit is not None:
        rows = rows[:limit]

    if fields is not None:
        fields = list(fields)
        for field in fields:
            if field not in columns and field not in defaults:
                raise ValueError(f'字段不存在: {field}')
        rows = [{'identity': row['identity'],
                 'values': {field: row['values'][field] for field in fields}}
                for row in rows]
    return rows


def compare(columns, head, *, where=None, sort=None, fields=None, limit=None,
            strategy='batch', fetch_strategy=None, batch_size=1024,
            budget=None):
    """执行按需查询并用慢速整行读取复核，返回结果与读取字节对照。"""
    counted = _CountingStore(columns.store)
    baseline = _baseline_rows(counted, head, where, sort, fields, limit)

    reader = ColumnReader(columns.store, head)
    try:
        with reader.query(where=where, sort=sort, fields=fields, limit=limit,
                          strategy=strategy, fetch_strategy=fetch_strategy,
                          batch_size=batch_size, budget=budget) as result:
            rows = list(result)
            report = result.report()
    finally:
        reader.close()

    optimized_bytes = report['bytes_read'] + report['metadata_bytes']
    return {
        'equal': rows == baseline,
        'rows': rows,
        'baseline': {
            'rows': len(baseline),
            'bytes_read': counted.bytes_read,
            'objects_read': counted.reads,
        },
        'optimized': {
            'rows': len(rows),
            'bytes_read': optimized_bytes,
            'column_bytes_read': report['bytes_read'],
            'metadata_bytes': report['metadata_bytes'],
            'blocks_read': report['blocks_read'],
            'report': report,
        },
        'saved_bytes': counted.bytes_read - optimized_bytes,
    }
