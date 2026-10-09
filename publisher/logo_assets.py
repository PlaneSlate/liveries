"""Append explicitly cleared logo variants to the existing SD library generation."""
import hashlib, io, json, re, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from PIL import Image


def _checked_logo(path,asset):
    if path.stat().st_size!=asset['bytes']:raise ValueError('Invalid logo size')
    data=path.read_bytes()
    if len(data)!=asset['bytes'] or hashlib.sha256(data).hexdigest()!=asset['sha256']:raise ValueError('Invalid logo checksum')
    with Image.open(io.BytesIO(data)) as im:
        if im.format!='PNG' or im.mode not in ('RGB','RGBA') or im.size!=(asset['width'],asset['height']):raise ValueError('Invalid logo dimensions/format')
        im.verify()
    return data


def _fetch_logo(asset,store,work,cache):
    sha=asset['sha256'];cached=cache/(sha+'.png') if cache else None
    check_seconds=0.;download_seconds=0.;downloaded=False
    def checked(path):
        nonlocal check_seconds
        started=perf_counter()
        try:return _checked_logo(path,asset)
        finally:check_seconds+=perf_counter()-started
    # Cache files are untrusted. Snapshot size, SHA and PNG metadata bind every hit.
    if cached and cached.is_file():
        try:return checked(cached),True,download_seconds,check_seconds,downloaded
        except (ValueError,OSError,SyntaxError):pass
    target=work/('logo-'+sha+'.png')
    if target.is_file():
        try:data=checked(target)
        except (ValueError,OSError,SyntaxError):target.unlink();data=None
    else:data=None
    if data is None:
        started=perf_counter()
        store.get(asset['key'],target,sha,asset['bytes'])
        download_seconds=perf_counter()-started;downloaded=True
        data=checked(target)
    if cached:
        temporary=None
        try:
            with NamedTemporaryFile(dir=cache,prefix=sha+'-',suffix='.tmp',delete=False) as f:
                temporary=Path(f.name);f.write(data)
            try:temporary.replace(cached)
            except OSError:
                # A simultaneous writer may already have installed this exact
                # object (Windows can deny replacement while it is being read).
                # Accept only a fully validated winning file; otherwise fail.
                checked(cached)
        finally:
            if temporary:temporary.unlink(missing_ok=True)
    return data,False,download_seconds,check_seconds,downloaded


def append_logos(archive_path,logos,store,work,logo_cache=None):
    if len(logos)>10000:raise ValueError('Too many logos')
    with zipfile.ZipFile(archive_path) as archive:
        contents={n:archive.read(n) for n in archive.namelist()}
    manifest=json.loads(contents['library/manifest.json']);index=manifest['index']['path']
    rows=[json.loads(line) for line in contents[index].splitlines()]
    has_logos=any(r.get('asset_type')=='airline_logo' for r in rows)
    if not logos and not has_logos:return
    rows=[r for r in rows if r.get('asset_type','livery')=='livery']
    prefix='library/generations/'+manifest['version']+'/'
    seen=set();assets={};entries=[]
    # Validate all mappings before submitting any downloads. One job per full SHA.
    for logo in logos:
        icao=logo.get('icao','')
        if not re.fullmatch('[A-Z]{3}',icao) or icao in seen:raise ValueError('Invalid or duplicate logo mapping')
        seen.add(icao)
        if logo.get('review_status')!='cleared' or not logo.get('review_note') or logo.get('import_status')!='valid':raise ValueError('Uncleared logo cannot be distributed')
        for layout,name,box in [('compact','compact',(48,48)),('large','detail',(192,96))]:
            asset=logo['variants'][name];sha=asset['sha256']
            if not re.fullmatch('[a-f0-9]{64}',sha) or asset['key']!='logos/variants/'+sha+'.png' or type(asset['bytes']) is not int or not 0<asset['bytes']<=512*1024:raise ValueError('Invalid logo reference')
            if any(type(asset.get(k)) is not int or not 0<asset[k]<=limit for k,limit in [('width',box[0]),('height',box[1])]):raise ValueError('Invalid logo dimensions/format')
            identity={k:asset[k] for k in ('key','sha256','bytes','width','height')}
            if sha in assets and assets[sha]!=identity:raise ValueError('Conflicting logo metadata')
            assets[sha]=identity;entries.append((icao,layout,identity))
    cache=Path(logo_cache) if logo_cache else None
    if cache:cache.mkdir(parents=True,exist_ok=True)
    started=perf_counter();verified={};hits=0;downloads=0;network_seconds=0.;validation_seconds=0.
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs={pool.submit(_fetch_logo,asset,store,Path(work),cache):sha for sha,asset in assets.items()}
        for i,job in enumerate(as_completed(jobs),1):
            data,hit,network,validation,downloaded=job.result();verified[jobs[job]]=data;hits+=hit
            network_seconds+=network;validation_seconds+=validation;downloads+=downloaded
            if i%100==0 or i==len(jobs):print('Verified logo assets:',i,'/',len(jobs),flush=True)
    print('Logo fetch + verification: unique=%d cache_hits=%d cache_misses=%d seconds=%.3f'%(len(assets),hits,len(assets)-hits,perf_counter()-started),flush=True)
    print('Logo timings: downloads=%d network_worker_seconds=%.3f validation_worker_seconds=%.3f'%(downloads,network_seconds,validation_seconds),flush=True)
    started=perf_counter()
    for icao,layout,asset in entries:
        sha=asset['sha256'];data=verified[sha]
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
    print('Logo archive update: mappings=%d seconds=%.3f'%(len(entries),perf_counter()-started),flush=True)
