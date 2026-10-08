"""Append explicitly cleared logo variants to the existing SD library generation."""
import hashlib, io, json, re, zipfile
from pathlib import Path
from PIL import Image


def append_logos(archive_path,logos,store,work):
    if len(logos)>10000:raise ValueError('Too many logos')
    with zipfile.ZipFile(archive_path) as archive:
        contents={n:archive.read(n) for n in archive.namelist()}
    manifest=json.loads(contents['library/manifest.json']);index=manifest['index']['path']
    rows=[json.loads(line) for line in contents[index].splitlines()]
    has_logos=any(r.get('asset_type')=='airline_logo' for r in rows)
    if not logos and not has_logos:return
    rows=[r for r in rows if r.get('asset_type','livery')=='livery']
    prefix='library/generations/'+manifest['version']+'/'
    seen=set()
    for logo in logos:
        icao=logo.get('icao','')
        if not re.fullmatch('[A-Z]{3}',icao) or icao in seen:raise ValueError('Invalid or duplicate logo mapping')
        seen.add(icao)
        if logo.get('review_status')!='cleared' or not logo.get('review_note') or logo.get('import_status')!='valid':raise ValueError('Uncleared logo cannot be distributed')
        for layout,name,box in [('compact','compact',(48,48)),('large','detail',(192,96))]:
            asset=logo['variants'][name];sha=asset['sha256']
            if not re.fullmatch('[a-f0-9]{64}',sha) or asset['key']!='logos/variants/'+sha+'.png' or not 0<asset['bytes']<=512*1024:raise ValueError('Invalid logo reference')
            target=work/('logo-'+sha+'.png')
            if not target.exists():store.get(asset['key'],target,sha,asset['bytes'])
            data=target.read_bytes()
            if len(data)!=asset['bytes'] or hashlib.sha256(data).hexdigest()!=sha:raise ValueError('Invalid logo checksum')
            with Image.open(io.BytesIO(data)) as im:
                if im.format!='PNG' or im.mode not in ('RGB','RGBA') or im.size!=(asset['width'],asset['height']) or im.width>box[0] or im.height>box[1]:raise ValueError('Invalid logo dimensions/format')
                im.verify()
            path=prefix+sha+'.png';contents[path]=data
            rows.append(dict(asset_type='airline_logo',review_status='cleared',type='LOGO',airline=icao,layout=layout,path=path,sha256=sha,bytes=len(data),width=asset['width'],height=asset['height']))
    contents[index]=b''.join((json.dumps(row,separators=(',',':'))+'\n').encode() for row in sorted(rows,key=lambda r:(r.get('asset_type','livery'),r['type'],r['airline'],r['layout'])))
    manifest['index'].update(bytes=len(contents[index]),sha256=hashlib.sha256(contents[index]).hexdigest(),entries=len(rows))
    contents['library/manifest.json']=(json.dumps(manifest,indent=2)+'\n').encode()
    referenced={r['path'] for r in rows}
    contents={n:v for n,v in contents.items() if not n.endswith('.png') or n in referenced}
    temporary=Path(archive_path).with_suffix('.logos.zip')
    with zipfile.ZipFile(temporary,'x',zipfile.ZIP_STORED) as archive:
        for name,data in sorted(contents.items()):archive.writestr(name,data)
    temporary.replace(archive_path)
