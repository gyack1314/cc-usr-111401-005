"""现有资料读取入口，以及按需查询的对照展示。"""
import json,tempfile,argparse
from pathlib import Path
from .objects import ObjectStore
from .columns import ColumnFile, inspect, open_reader
from .reader import compare

def _sort_spec(text):
    keys=[]
    for item in text.split(','):
        item=item.strip()
        if not item:continue
        field,_,direction=item.partition(':')
        keys.append((field.strip(),direction.strip().lower() or 'asc'))
    return keys

def _compare(args,data,store):
    service=ColumnFile(store);head=service.create(data)
    fields=None if not args.fields else [f for f in args.fields.split(',') if f]
    sort=None if not args.sort else _sort_spec(args.sort)
    where=None if not args.where else json.loads(args.where)
    if args.selection:
        ordinals=[int(x) for x in args.selection.split(',') if x!='']
        reader=open_reader(store,head)
        try:
            selection=reader.selection(ordinals,allow_deleted=True)
            rows=list(reader.fetch(selection,fields))
            output={'rows':rows,'stats':reader.stats.report()}
        finally:
            reader.close()
        print(json.dumps(output,ensure_ascii=False,sort_keys=True,default=list))
        return
    print(json.dumps(compare(service,head,where=where,sort=sort,fields=fields,
                             limit=args.limit,strategy=args.strategy,
                             batch_size=args.batch_size),
                     ensure_ascii=False,sort_keys=True,default=list))

def main():
    parser=argparse.ArgumentParser(description='读取并核对本地资料')
    parser.add_argument('--source',default='data/source.json')
    parser.add_argument('--compare',action='store_true',
                        help='对照按需列块读取与既有整行读取')
    parser.add_argument('--where',default=None,help='JSON 过滤表达式')
    parser.add_argument('--sort',default=None,help='逗号分隔排序字段，可加 :desc')
    parser.add_argument('--fields',default=None,help='逗号分隔输出字段')
    parser.add_argument('--limit',type=int,default=None)
    parser.add_argument('--selection',default=None,
                        help='逗号分隔的物理 ordinal，按顺序回取，允许重复')
    parser.add_argument('--strategy',default='batch',choices=('batch','intervals'))
    parser.add_argument('--batch-size',type=int,default=1024)
    args=parser.parse_args()
    data=json.loads(Path(args.source).read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);store=ObjectStore(root/'objects')
        if args.compare or args.where or args.selection or args.fields or args.sort:
            _compare(args,data,store)
            return
        result=inspect(data,store,root)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
if __name__=='__main__':main()
