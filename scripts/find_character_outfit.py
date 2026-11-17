"""按角色、服装、形态查询已验收的新服装素材；只读，不调用生图或业务服务。"""
from pathlib import Path
import argparse
import hashlib
import json
import sys

DEFAULT_INDEX=Path(__file__).resolve().parents[1]/'assets/ai-character-references/角色素材索引.json'

def normalized(value):
    return ''.join(str(value).split()).casefold()

def unique(items, value, keys):
    matches=[item for item in items if normalized(value) in {normalized(item.get(k,'')) for k in keys}]
    if len(matches)!=1:
        raise ValueError('名称不存在或不唯一：'+value)
    return matches[0]

def lookup(index_path, character, outfit, form):
    index_path=Path(index_path).resolve()
    data=json.loads(index_path.read_text(encoding='utf-8-sig'))
    role=unique(data['characters'],character,('id','name'))
    clothes=unique(role['outfits'],outfit,('id','name','title'))
    forms={'normal':'正常版','正常':'正常版','正常版':'正常版','q':'Q版','q版':'Q版'}
    selected=forms.get(normalized(form))
    if not selected:
        raise ValueError('形态须为正常版或Q版')
    image=clothes['images'].get(selected)
    if not image or image.get('status')!='accepted':
        raise ValueError('该素材尚未验收；不回退到其他服装或原始参考')
    root=index_path.parent
    path=(root/image['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('索引路径无效')
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=image['sha256']:
        raise ValueError('素材文件与已验收散列不一致')
    return {'角色':role['name'],'服装':clothes['name'],'设计名称':clothes['title'],
            '形态':selected,'图片路径':str(path),'相对路径':image['path'],
            '状态':'已验收','SHA256':actual,'宽':image['width'],'高':image['height']}

def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--character',required=True,help='角色姓名或唯一标识')
    p.add_argument('--outfit',required=True,help='服装类别、设计名称或唯一标识')
    p.add_argument('--form',required=True,help='正常版或Q版')
    p.add_argument('--index',type=Path,default=DEFAULT_INDEX)
    args=p.parse_args()
    try:
        result=lookup(args.index,args.character,args.outfit,args.form)
    except (ValueError,KeyError,OSError) as error:
        print(json.dumps({'错误':str(error)},ensure_ascii=False),file=sys.stderr)
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
