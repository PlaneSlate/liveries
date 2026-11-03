"""Add captured airline tails to the shared large artwork generation for Plus.

Micro keeps its existing compact pack. Mini ignores the TAIL asset family;
Plus uses it through the existing verified, atomic content update.
"""
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from PIL import Image


def validate_tails(tails):
    if not isinstance(tails,list) or len(tails)>10000:
        raise ValueError('Invalid captured airline tails')
    seen=set();total=0
    for tail in tails:
        if (not isinstance(tail,dict) or set(tail)!={'icao','key','sha256','bytes','width','height'}
                or not re.fullmatch('[A-Z]{3}',tail.get('icao','')) or tail['icao'] in seen
                or not re.fullmatch('[a-f0-9]{64}',tail.get('sha256',''))
                or tail.get('key')!='logos/sets/'+tail['sha256']+'.png'
                or type(tail.get('bytes')) is not int or not 0<tail['bytes']<=4*1024**2
                or any(type(tail.get(k)) is not int or not 0<tail[k]<=2048 for k in ('width','height'))):
            raise ValueError('Invalid captured airline tail reference')
        seen.add(tail['icao']);total+=tail['bytes']
    if total>128*1024**2:raise ValueError('Airline tails exceed content budget')


def append_tails(archive_path,tails,store,work):
    validate_tails(tails)
    archive_path=Path(archive_path);work=Path(work);work.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        contents={name:archive.read(name) for name in archive.namelist()}
    manifest=json.loads(contents['library/manifest.json']);index=manifest['index']['path']
    rows=[json.loads(raw) for raw in contents[index].splitlines()]
    rows=[row for row in rows if row.get('type')!='TAIL']
    prefix='library/generations/'+manifest['version']+'/'
    verified={}
    for tail in sorted(tails,key=lambda row:row['icao']):
        digest=tail['sha256']
        if digest not in verified:
            path=work/(digest+'.png');store.get(tail['key'],path,digest,tail['bytes'])
            data=path.read_bytes()
            if len(data)!=tail['bytes'] or hashlib.sha256(data).hexdigest()!=digest:
                raise ValueError('Airline tail checksum mismatch')
            with Image.open(io.BytesIO(data)) as image:
                if image.format!='PNG' or image.mode!='RGBA' or image.size!=(tail['width'],tail['height']):
                    raise ValueError('Invalid airline tail PNG')
                image.load()
                if not image.getchannel('A').getbbox():raise ValueError('Empty airline tail PNG')
            verified[digest]=(data,tail['width'],tail['height'])
        data,width,height=verified[digest]
        if (width,height)!=(tail['width'],tail['height']) or len(data)!=tail['bytes']:
            raise ValueError('Conflicting airline tail metadata')
        path=prefix+'tail-'+digest+'.png';contents[path]=data
        rows.append(dict(type='TAIL',airline=tail['icao'],layout='large',asset_type='airline_logo',
                         review_status='cleared',path=path,sha256=digest,bytes=len(data),width=width,height=height))
    contents[index]=b''.join((json.dumps(row,separators=(',',':'))+'\n').encode() for row in sorted(rows,key=lambda row:(row['type'],row['airline'],row['layout'],row.get('source_filename',''))))
    manifest['index'].update(bytes=len(contents[index]),entries=len(rows),sha256=hashlib.sha256(contents[index]).hexdigest())
    # This marker also makes deletion authoritative: never revive a legacy USB tail.
    manifest['airline_tails']=True
    contents['library/manifest.json']=(json.dumps(manifest,indent=2)+'\n').encode()
    referenced={row['path'] for row in rows}
    temporary=archive_path.with_suffix('.tails.zip')
    with zipfile.ZipFile(temporary,'x',zipfile.ZIP_STORED) as archive:
        for name,data in sorted(contents.items()):
            if not name.endswith('.png') or name in referenced:
                info=zipfile.ZipInfo(name,(2026,10,15,0,0,0));info.compress_type=zipfile.ZIP_STORED;archive.writestr(info,data)
    temporary.replace(archive_path)
