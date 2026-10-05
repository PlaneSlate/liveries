"""Convert a flat artwork ZIP using authoritative mappings, retaining the base library."""
from pathlib import Path, PurePosixPath
import argparse
import io
import json
import re
import sys
import zipfile
import stat
from PIL import Image
from sd_manager import Package, sha, compact


def convert(source, base, output, version, types, airlines, *, file_pairs=None):
    if not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', version):
        raise ValueError('Invalid version')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    prefix = f'library/generations/{version}/'
    library = Package(base)
    if library.kind != 'library':
        library.close()
        raise ValueError('Base must be a livery library')
    audit = dict(source_files=0, mapped_files=0, unmapped=[], conflicts=[], changed_keys=0, new_keys=0)
    try:
        with zipfile.ZipFile(source) as raw, zipfile.ZipFile(output/'liveries.zip', 'x', zipfile.ZIP_DEFLATED) as result:
            members = raw.infolist()
            if len(members)>5000 or sum(m.file_size for m in members)>2*1024**3:
                raise ValueError('Artwork archive exceeds limits')
            names=set()
            for m in members:
                p=PurePosixPath(m.filename)
                if (len(p.parts)!=1 or p.name!=m.filename or '\\' in m.filename or ':' in m.filename
                        or m.filename.startswith('.') or not m.filename.lower().endswith('.png')
                        or stat.S_ISLNK(m.external_attr>>16) or m.flag_bits&1
                        or m.file_size>20*1024**2 or m.filename.casefold() in names):
                    raise ValueError('Expected unique flat PNG files without unsafe paths')
                names.add(m.filename.casefold())
            index={}
            assets={}
            for line in library.read(library.manifest['index']['path'],64*1024**2).splitlines():
                entry=json.loads(line)
                assets[prefix+entry['sha256']+'.png'] = entry['path']
                entry['path']=prefix+entry['sha256']+'.png'
                index[(entry['type'],entry['airline'],entry['layout'])]=entry
            new_assets={}; supplied={}
            for member in members:
                audit['source_files']+=1
                stem=PurePosixPath(member.filename).stem
                kind,sep,airline=stem.partition('_')
                codes=types.get(kind.strip().casefold(),[])
                operators=['*'] if airline.strip().casefold()=='unknown' else airlines.get(airline.strip().casefold(),[])
                pairs = (file_pairs.get(member.filename, []) if file_pairs is not None
                         else [(code, operator) for code in codes for operator in operators])
                if any(not re.fullmatch('[A-Z0-9]{1,4}', code) or not re.fullmatch(r'[A-Z]{3}|\*', operator) for code,operator in pairs):
                    raise ValueError('Invalid authoritative artwork mapping')
                if not sep or not pairs:
                    audit['unmapped'].append(member.filename);continue
                with Image.open(io.BytesIO(raw.read(member))) as image:
                    if image.format!='PNG' or image.width*image.height>16_000_000:
                        raise ValueError('Invalid artwork image')
                    original=image.convert('RGBA')
                    bounds=original.getchannel('A').getbbox()
                    if not bounds: raise ValueError('Empty artwork')
                    original=original.crop(bounds)
                prepared=[]
                for layout,size in [('compact',(300,88)),('large',(640,180))]:
                    image=original.copy();image.thumbnail(size,Image.Resampling.LANCZOS)
                    stream=io.BytesIO();image.save(stream,format='PNG',compress_level=9)
                    data=stream.getvalue();digest=sha(data);path=prefix+digest+'.png'
                    new_assets[path]=data
                    for code,operator in pairs:
                        entry=dict(type=code,airline=operator,layout=layout,path=path,sha256=digest,bytes=len(data),width=image.width,height=image.height)
                        key=(code,operator,layout)
                        if key in supplied and supplied[key]!=digest:
                            raise ValueError(f'Conflicting artwork for {key}')
                        supplied[key]=digest;prepared.append((key,entry))
                for key,entry in prepared:
                    if key not in index: audit['new_keys']+=1
                    elif index[key]['sha256']!=entry['sha256']: audit['changed_keys']+=1
                    index[key]=entry
                audit['mapped_files']+=1
            needed={e['path'] for e in index.values()}
            for path in sorted(needed):
                result.writestr(path,new_assets[path] if path in new_assets else library.read(assets[path],512*1024))
            data=''.join(compact(index[k])+'\n' for k in sorted(index)).encode()
            index_path=prefix+'index.jsonl';result.writestr(index_path,data)
            manifest=dict(schema=1,version=version,mapping_version=library.manifest['mapping_version'],
                          index=dict(path=index_path,bytes=len(data),sha256=sha(data),entries=len(index)))
            result.writestr('library/manifest.json',compact(manifest))
            audit['index_entries']=len(index)
        checked=Package(output/'liveries.zip');checked.close()
        (output/'import-report.json').write_text(json.dumps(audit,indent=2,ensure_ascii=False),encoding='utf-8')
        return audit
    finally:
        library.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','base','output','version'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from planeslate.core.adsb.airlines import AIRLINE_MAP
    from planeslate.core.adsb.aircraft_types import AIRCRAFT_LIVERY_TYPE_MAP
    types={};airlines={}
    for code,name in AIRCRAFT_LIVERY_TYPE_MAP.items():
        if name and re.fullmatch('[A-Z0-9]{1,4}',code): types.setdefault(name.strip().casefold(),[]).append(code)
    for code,entry in AIRLINE_MAP.items():
        if re.fullmatch('[A-Z]{3}',code) and entry.livery_name:
            airlines.setdefault(entry.livery_name.strip().casefold(),[]).append(code)
    report=convert(args.source,args.base,args.output,args.version,types,airlines)
    print(json.dumps({k:v for k,v in report.items() if k not in ('unmapped','conflicts')}))
