"""Verified differential device payloads; full recovery payloads stay available."""
import hashlib
import json
import re
from pathlib import Path


def encoded(value):
    return (json.dumps(value, separators=(',', ':'))+'\n').encode()


def records(path):
    """Validate a full stream and retain bounded metadata, not image contents."""
    result=[];plan=hashlib.sha256()
    with Path(path).open('rb') as stream:
        raw=stream.readline(8193)
        header=json.loads(raw)
        if len(raw)>8192 or header['format']!='planeslate-update-v1' or not 0<header['files']<=100001 or not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?',header['version']):
            raise ValueError('Invalid full update header')
        if hashlib.sha256(header['manifest'].encode()).hexdigest()!=header['manifest_sha256']:
            raise ValueError('Invalid full manifest')
        prefix='library/generations/'+header['version']+'/'
        seen=set()
        for _ in range(header['files']):
            raw=stream.readline(8193);entry=json.loads(raw)
            name=entry['path'];size=entry['bytes']
            if len(raw)>8192 or not name.startswith(prefix) or '..' in name.split('/') or name in seen or not 0<size<=64*1024**2:
                raise ValueError('Invalid full update record')
            seen.add(name);offset=stream.tell();sha=hashlib.sha256();left=size
            while left:
                block=stream.read(min(left,65536))
                if not block:raise ValueError('Truncated full update')
                sha.update(block);left-=len(block)
            if sha.hexdigest()!=entry['sha256']:raise ValueError('Invalid full file checksum')
            plan.update(f"{name}\t{size}\t{entry['sha256']}\n".encode())
            result.append(dict(entry,offset=offset))
        if stream.read(1) or plan.hexdigest()!=header['plan_sha256']:
            raise ValueError('Invalid full update plan')
    return header,result


def build_delta(current,previous,output):
    new,files=records(current);old,bases=records(previous)
    if old['product']!=new['product'] or old['version']==new['version']:
        raise ValueError('Invalid delta baseline')
    reusable={(e['sha256'],e['bytes']):e['path'] for e in bases if e['path'].endswith('.png')}
    header=dict(new,format='planeslate-update-v2',base_version=old['version'],
                base_manifest_sha256=old['manifest_sha256'])
    reused=0;transferred=0
    with Path(output).open('xb') as dest,Path(current).open('rb') as source:
        dest.write(encoded(header))
        for item in files:
            entry={k:v for k,v in item.items() if k!='offset'}
            reuse=reusable.get((item['sha256'],item['bytes'])) if item['path'].endswith('.png') else None
            if reuse:
                entry['reuse']=reuse;reused+=1
            else:transferred+=1
            dest.write(encoded(entry))
            if not reuse:
                source.seek(item['offset']);left=item['bytes']
                while left:
                    block=source.read(min(left,65536))
                    if not block:raise ValueError('Source changed during delta build')
                    dest.write(block);left-=len(block)
    verify_delta(output,current,previous)
    return dict(base_version=old['version'],base_manifest_sha256=old['manifest_sha256'],
                reused_files=reused,transferred_files=transferred)


def verify_delta(delta,current,previous):
    """Reconstruct the exact approved full body, including reused image bytes."""
    new,files=records(current);old,bases=records(previous)
    by_path={e['path']:e for e in bases}
    with Path(delta).open('rb') as stream,Path(previous).open('rb') as source:
        header=json.loads(stream.readline(8193))
        if header!=dict(new,format='planeslate-update-v2',base_version=old['version'],base_manifest_sha256=old['manifest_sha256']):
            raise ValueError('Delta header differs')
        for item in files:
            entry=json.loads(stream.readline(8193));reuse=entry.pop('reuse',None)
            if entry!={k:v for k,v in item.items() if k!='offset'}:raise ValueError('Delta plan differs')
            reader=stream
            if reuse:
                old_file=by_path.get(reuse)
                if not old_file or not reuse.endswith('.png') or not item['path'].endswith('.png') or (old_file['bytes'],old_file['sha256'])!=(item['bytes'],item['sha256']):
                    raise ValueError('Invalid reuse reference')
                source.seek(old_file['offset']);reader=source
            sha=hashlib.sha256();left=item['bytes']
            while left:
                block=reader.read(min(left,65536))
                if not block:raise ValueError('Truncated delta')
                sha.update(block);left-=len(block)
            if sha.hexdigest()!=item['sha256']:raise ValueError('Delta checksum mismatch')
        if stream.read(1):raise ValueError('Trailing delta data')


def add_deltas(upload,previous):
    upload=Path(upload);previous=Path(previous)
    feed=json.loads((upload/'device-updates.json').read_text())
    for product in feed['products']:
        if product['id'] not in ('mini_800','micro_360'):continue
        base=previous/(product['id']+'.psu')
        if not base.exists():continue
        name=f"{product['id']}-{feed['version']}-delta.psu"
        target=upload/name
        info=build_delta(upload/product['file'],base,target)
        if target.stat().st_size>=product['bytes']:
            target.unlink();continue
        with target.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        product['delta']=dict(info,format='planeslate-update-v2',file=name,bytes=target.stat().st_size,
            sha256=checksum,url=product['url'].rsplit('/',1)[0]+'/'+name)
    (upload/'device-updates.json').write_text(json.dumps(feed,indent=2)+'\n',encoding='utf8')
    return feed
