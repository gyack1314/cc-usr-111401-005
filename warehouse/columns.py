"""独立列块的规范存储及完整行慢速读取。"""

class ColumnFile:
    def __init__(self,store):self.store=store
    def create(self,data):
        count=len(next(iter(data['columns'].values())));columns={}
        for field,values in data['columns'].items():
            if len(values)!=count:raise ValueError('列行数不一致')
            blocks=[];start=0
            for end in data['column_blocks'][field]:
                if not start<end<=count:raise ValueError('列块区间非法')
                blocks.append({'start':start,'end':end,'digest':self.store.put_json(values[start:end])});start=end
            if start!=count:raise ValueError('列块未覆盖全部行')
            columns[field]=blocks
        if any(i<0 or i>=count for i in data['deleted']):raise ValueError('删除位置非法')
        return self.store.put_json({'file_id':'f0','rowgroup':0,'count':count,'columns':columns,'deleted':data['deleted'],'defaults':{}})
    def load_column(self,head,field):
        m=self.store.json(head);values=[]
        if field not in m['columns']:
            if field not in m['defaults']:raise ValueError('字段不存在')
            return [m['defaults'][field]]*m['count']
        for b in m['columns'][field]:
            chunk=self.store.json(b['digest'])
            if len(chunk)!=b['end']-b['start']:raise ValueError('列块行数损坏')
            values.extend(chunk)
        return values
    def read_rows(self,head):
        m=self.store.json(head);cols={f:self.load_column(head,f) for f in m['columns']}
        for i in range(m['count']):
            if i not in m['deleted']:
                yield {'identity':[head,m['file_id'],m['rowgroup'],i],'values':{f:v[i] for f,v in cols.items()}}

def inspect(data,store,root):
    service=ColumnFile(store);head=service.create(data);rows=list(service.read_rows(head))
    return {'snapshot':head,'rows':rows,'column_count':len(data['columns'])}
