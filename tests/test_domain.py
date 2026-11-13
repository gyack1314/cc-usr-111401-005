"""领域资料及现有接口核对。"""
import json,tempfile,unittest
from pathlib import Path
from warehouse.objects import ObjectStore
from warehouse.columns import inspect

class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=ObjectStore(self.root/'objects')
        self.data=json.loads(Path('data/source.json').read_text(encoding='utf-8'))
    def test_existing_reader(self):
        result=inspect(self.data,self.store,self.root)
        self.assertTrue(result)

    def test_physical_positions_survive_delete(self):
        from warehouse.columns import ColumnFile
        c=ColumnFile(self.store);head=c.create(self.data);rows=list(c.read_rows(head))
        self.assertEqual([r['identity'][-1] for r in rows],[0,2,3,4,5]);self.assertEqual(rows[1]['values']['30'],'long-c')
