"""按需列块读取层的行为核对。"""
import json, tempfile, unittest
from pathlib import Path
from warehouse.objects import ObjectStore
from warehouse.columns import ColumnFile, open_reader
from warehouse.reader import (Budget, BudgetExceeded, ColumnReader, Selection,
                              SnapshotMismatch, compare, evaluate)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=ObjectStore(self.root/'objects')
        self.data=json.loads(Path('data/source.json').read_text(encoding='utf-8'))
        self.columns=ColumnFile(self.store);self.head=self.columns.create(self.data)
        self.reader=open_reader(self.store,self.head)

    def tearDown(self):
        self.reader.close()

    # -- 物理身份 ---------------------------------------------------------

    def test_selection_keeps_order_and_duplicates(self):
        selection=self.reader.selection([5,0,5,2])
        rows=list(self.reader.fetch(selection,['10','30'],strategy='intervals'))
        self.assertEqual([r['identity'][-1] for r in rows],[5,0,5,2])
        self.assertEqual([r['values']['10'] for r in rows],[6,1,6,3])
        self.assertEqual(rows[0],rows[2])

    def test_selection_ordinals_are_not_renumbered(self):
        selection=self.reader.selection([0,2])
        self.assertEqual(list(selection),[0,2])
        self.assertEqual(selection[1],2)
        self.assertEqual(selection.identities[1][3],2)

    def test_selection_across_unaligned_blocks(self):
        # 三列各自的列块边界是 [2,4,6] / [3,6] / [1,5,6]。
        selection=self.reader.selection([0,2,3,4,5])
        rows=list(self.reader.fetch(selection,['10','20','30'],strategy='intervals'))
        self.assertEqual([r['values']['20'] for r in rows],['a','a',None,'b','a'])
        self.assertEqual([r['values']['30'] for r in rows],
                         ['long-a','long-c','long-d','long-e','long-f'])

    def test_deleted_rows_are_not_selected(self):
        self.assertEqual(self.reader.live_ordinals(),(0,2,3,4,5))
        with self.assertRaises(ValueError):
            self.reader.selection([1])

    def test_explicit_deleted_selection_roundtrips(self):
        selection=self.reader.selection([1],allow_deleted=True)
        rows=list(self.reader.fetch(selection,['30']))
        self.assertEqual(rows[0]['identity'][-1],1)
        self.assertEqual(rows[0]['values']['30'],'long-b')

    def test_selection_from_other_snapshot_fails(self):
        other=ColumnFile(self.store);other_head=other.create(
            {'columns':{'10':[9]},'column_blocks':{'10':[1]},'deleted':[]})
        other_reader=open_reader(self.store,other_head)
        foreign=other_reader.selection([0])
        with self.assertRaises(SnapshotMismatch):
            list(self.reader.fetch(foreign,['10']))

    def test_damaged_block_fails_loudly(self):
        manifest=self.store.json(self.head)
        digest=manifest['columns']['30'][1]['digest']
        (self.store.root/digest).write_bytes(b'[1]')
        selection=self.reader.selection([4])
        with self.assertRaises(ValueError):
            list(self.reader.fetch(selection,['30']))

    # -- 三值逻辑 ---------------------------------------------------------

    def test_three_valued_filter(self):
        self.assertIsNone(evaluate(['=', '20', 'a'], {'20': None}))
        self.assertIsNone(evaluate(['not', ['=', '20', 'a']], {'20': None}))
        self.assertEqual(evaluate(['or', ['is null', '20'], ['=', '20', 'a']],
                                  {'20': None}), True)
        self.assertEqual(evaluate(['and', ['is null', '20'], ['=', '20', 'a']],
                                  {'20': 'b'}), False)
        self.assertIsNone(evaluate(['and', ['is null', '20'], ['=', '20', 'a']],
                                   {'20': None}))
        self.assertIsNone(evaluate(['<', '20', 'a'], {'20': None}))
        self.assertIs(evaluate(['is not null', '20'], {'20': None}), False)
        selection=self.reader.select(['=', '20', 'a'])
        # 只有 TRUE 保留，UNKNOWN 行不进入选择向量。
        self.assertEqual(list(selection),[0,2,5])

    def test_filter_never_keeps_null(self):
        selection=self.reader.select({'op':'is not null','field':'20'})
        self.assertEqual(list(selection),[0,2,4,5])

    # -- 读放大 -----------------------------------------------------------

    def test_projection_reads_only_needed_blocks(self):
        with self.reader.query(where=['=','20','a'],fields=['10']) as result:
            rows=list(result);report=result.report()
        self.assertEqual([r['values'] for r in rows],[{'10':1},{'10':3},{'10':6}])
        self.assertEqual(report['fields_read'],['10','20'])
        # 列 20 需要两块；列 10 需要被选中行所在的 3 块，没有碰列 30。
        self.assertEqual(report['blocks_read'],5)

    def test_filter_columns_are_read_before_output_columns(self):
        with self.reader.query(where=['=','20','a'],sort=['30'],fields=['10'],
                               fetch_strategy='intervals') as result:
            list(result);report=result.report()
        order=[entry['field'] for entry in report['column_blocks']]
        self.assertLess(order.index('20'),order.index('30'))
        self.assertLess(order.index('30'),order.index('10'))
        self.assertEqual(report['fields_read'],['10','20','30'])

    def test_projection_beats_full_row_read(self):
        result=compare(self.columns,self.head,where=['=','20','a'],
                       fields=['10'],limit=1)
        self.assertTrue(result['equal'])
        self.assertLess(result['optimized']['bytes_read'],
                        result['baseline']['bytes_read'])

    def test_batch_and_intervals_agree(self):
        batch=compare(self.columns,self.head,where=['=','20','a'],sort=['30'],
                      fields=['10','30'],strategy='batch',batch_size=2)
        intervals=compare(self.columns,self.head,where=['=','20','a'],sort=['30'],
                          fields=['10','30'],strategy='intervals')
        self.assertTrue(batch['equal']);self.assertTrue(intervals['equal'])
        self.assertEqual(batch['rows'],intervals['rows'])

    def test_strategy_schedules_do_not_change_result(self):
        selection=self.reader.selection([5,0,5,2,3,4])
        for strategy in ('batch','intervals'):
            rows=list(self.reader.fetch(selection,['10','20','30'],
                                        strategy=strategy,batch_size=2))
            self.assertEqual([r['identity'][-1] for r in rows],[5,0,5,2,3,4])

    # -- 预算与提前关闭 ---------------------------------------------------

    def test_budget_rejects_before_reading(self):
        reader=open_reader(self.store,self.head,budget=Budget(max_blocks=1))
        try:
            with self.assertRaises(BudgetExceeded):
                list(reader.query(where=['=','20','a'],fields=['20']))
            self.assertEqual(reader.last_stats.blocks_read,1)
        finally:
            reader.close()

    def test_budget_limits_sort_fetch(self):
        reader=open_reader(self.store,self.head,budget=Budget(max_blocks=3))
        try:
            with self.assertRaises(BudgetExceeded):
                list(reader.query(where=['=','20','a'],sort=['30'],fields=['10']))
        finally:
            reader.close()

    def test_limit_stops_without_reading_all_blocks(self):
        with self.reader.query(where=['=', '20', 'a'], fields=['30'],
                               limit=1) as result:
            rows=list(result);report=result.report()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['identity'][-1],0)
        self.assertEqual(report['rows_output'],1)
        self.assertEqual(report['rows_examined'],1)
        self.assertLess(report['blocks_read'],4)

    def test_early_close_releases_generator(self):
        result=self.reader.query(fields=['10','20','30'],strategy='batch',
                                 batch_size=1)
        next(iter(result))
        result.close()
        with self.assertRaises(RuntimeError):
            next(result)
        self.assertEqual(self.reader._cache,{})

    def test_zero_limit_reads_nothing(self):
        with self.reader.query(where=['=','20','a'],fields=['10'],limit=0) as result:
            self.assertEqual(list(result),[])
            self.assertEqual(result.report()['blocks_read'],0)

    def test_reader_close_is_idempotent(self):
        reader=open_reader(self.store,self.head)
        reader.close();reader.close()
        with self.assertRaises(RuntimeError):
            list(reader.query())

    # -- 缺省列 -----------------------------------------------------------

    def test_missing_column_uses_snapshot_default(self):
        data=json.loads(json.dumps(self.data))
        del data['columns']['30'];del data['column_blocks']['30']
        head=self.columns.create(data)
        # 手工补上缺省定义，模拟旧文件 schema。
        manifest=self.store.json(head);manifest['defaults']={'30':'n/a'}
        head=self.store.put_json(manifest)
        reader=open_reader(self.store,head)
        try:
            rows=list(reader.fetch(reader.selection([0,2]),['10','30']))
            self.assertEqual([r['values']['30'] for r in rows],['n/a','n/a'])
            # 缺省列不产生列块读取；ordinal 0 和 2 落在列 10 的两个列块。
            self.assertEqual(reader.last_stats.blocks_read,2)
            self.assertEqual([entry['field'] for entry in
                              reader.last_stats.column_blocks],['10','10'])
        finally:
            reader.close()

    def test_default_column_in_filter(self):
        data=json.loads(json.dumps(self.data))
        del data['columns']['20'];del data['column_blocks']['20']
        head=self.columns.create(data)
        manifest=self.store.json(head);manifest['defaults']={'20':'a'}
        head=self.store.put_json(manifest)
        reader=open_reader(self.store,head)
        try:
            self.assertEqual(list(reader.select(['=','20','a'])),[0,2,3,4,5])
            self.assertEqual(reader.last_stats.bytes_read,0)
        finally:
            reader.close()

    def test_unknown_column_fails(self):
        with self.assertRaises(ValueError):
            list(self.reader.query(fields=['99']))
        with self.assertRaises(ValueError):
            self.reader.select(['=','99',1])


if __name__=='__main__':
    unittest.main()
