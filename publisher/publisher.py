"""Build an immutable R2 snapshot and publish a verified GitHub release.

The default mode only builds locally. --publish is used solely by the explicit
workflow_dispatch job. R2 credentials have read-only access to one bucket.
"""
import argparse
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.parse
import zipfile
from datetime import datetime, timezone
from collections import defaultdict
from PIL import Image
from import_livery_artwork import convert
from build_livery_release import build
from livery_distribution import prepare_downloads, GitHubPublisher
from sd_manager import Package
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from time import perf_counter
import threading

REPO='PlaneSlate/liveries'
HEX=re.compile(r'[a-f0-9]{64}')


@contextmanager
def phase(name):
    start=perf_counter()
    print('Phase:',name,'started',flush=True)
    try:
        yield
    finally:
        print('Phase:',name,'seconds=%.3f'%(perf_counter()-start),flush=True)


class CachedOriginals:
    """Untrusted cache: size + SHA checked on every hit; PNG checks still run downstream."""
    def __init__(self,store,root):
        self.store=store;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.Lock();self.locks={};self.hits=0;self.misses=0

    def get(self,key,target,checksum,max_bytes):
        if not HEX.fullmatch(checksum):raise ValueError('Invalid cache checksum')
        if key!='originals/sha256/'+checksum+'.png':
            return self.store.get(key,target,checksum,max_bytes)
        with self.lock:
            guard=self.locks.setdefault(checksum,threading.Lock())
        with guard:
            cached=self.root/(checksum+'.png')
            if cached.is_file() and cached.stat().st_size==max_bytes and digest(cached)==checksum:
                shutil.copyfile(cached,target)
                # Bind copied bytes too, including changes during the copy.
                if target.stat().st_size!=max_bytes or digest(target)!=checksum:
                    raise ValueError('Cache changed during copy')
                with self.lock:self.hits+=1
                return
            self.store.get(key,target,checksum,max_bytes)
            if target.stat().st_size!=max_bytes or digest(target)!=checksum:
                raise ValueError('Invalid original for cache')
            temporary=cached.with_suffix('.tmp')
            shutil.copyfile(target,temporary);temporary.replace(cached)
            with self.lock:self.misses+=1


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def normalize_zip(path):
    temporary=path.with_suffix('.normalized.zip')
    # PNG members already contain compressed data; avoid expensive second compression.
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(temporary,'x',zipfile.ZIP_DEFLATED,compresslevel=1) as target:
        for name in sorted(source.namelist()):
            info=zipfile.ZipInfo(name,(2020,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            target.writestr(info,source.read(name),compresslevel=1)
    temporary.replace(path)


def valid_name(name):
    return isinstance(name,str) and 1<len(name)<=180 and name==name.strip() and name.endswith('.png') and '_' in name and not re.search(r'[\x00-\x1f\x7f/\\:<>"|?*]',name) and '..' not in name


class R2:
    def __init__(self):
        endpoint=urllib.parse.urlsplit(os.environ['R2_ENDPOINT'])
        if endpoint.scheme!='https' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/') or not re.fullmatch(r'[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com',endpoint.netloc):
            raise ValueError('Invalid R2 endpoint')
        self.host=endpoint.hostname
        self.bucket=os.environ['R2_BUCKET']
        if self.bucket!='planeslate-livery-originals':
            raise ValueError('Unexpected bucket')
        self.connections=threading.local()

    def get(self,key,target,checksum,max_bytes):
        if not HEX.fullmatch(checksum) or not re.fullmatch(r'[a-zA-Z0-9_./-]+',key) or '..' in key or key.startswith('/'):
            raise ValueError('Invalid object reference')
        now=datetime.now(timezone.utc)
        day=now.strftime('%Y%m%d'); stamp=now.strftime('%Y%m%dT%H%M%SZ')
        path='/'+self.bucket+'/'+urllib.parse.quote(key,safe='/')
        empty=hashlib.sha256(b'').hexdigest()
        headers=f'host:{self.host}\nx-amz-content-sha256:{empty}\nx-amz-date:{stamp}\n'
        names='host;x-amz-content-sha256;x-amz-date'
        canonical='GET\n'+path+'\n\n'+headers+'\n'+names+'\n'+empty
        scope=day+'/auto/s3/aws4_request'
        string='AWS4-HMAC-SHA256\n'+stamp+'\n'+scope+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
        secret=('AWS4'+os.environ['R2_SECRET_ACCESS_KEY']).encode()
        for part in (day,'auto','s3','aws4_request'):
            secret=hmac.new(secret,part.encode(),hashlib.sha256).digest()
        signature=hmac.new(secret,string.encode(),hashlib.sha256).hexdigest()
        auth=f"AWS4-HMAC-SHA256 Credential={os.environ['R2_ACCESS_KEY_ID']}/{scope}, SignedHeaders={names}, Signature={signature}"
        c=getattr(self.connections,'client',None)
        if c is None:
            c=http.client.HTTPSConnection(self.host,timeout=120)
            self.connections.client=c
        try:
            c.request('GET',path,headers={'Authorization':auth,'x-amz-date':stamp,'x-amz-content-sha256':empty})
            r=c.getresponse()
            if r.status!=200:
                raise ValueError('Original storage returned HTTP '+str(r.status))
            total=0
            with target.open('xb') as f:
                while data:=r.read(65536):
                    total+=len(data)
                    if total>max_bytes:
                        raise ValueError('Object exceeds expected size')
                    f.write(data)
            if digest(target)!=checksum:
                raise ValueError('Object checksum mismatch')
        except BaseException:
            c.close();self.connections.client=None
            raise


class LocalR2:
    def __init__(self,root): self.root=Path(root).resolve()
    def get(self,key,target,checksum,max_bytes):
        source=(self.root/key).resolve()
        if not source.is_relative_to(self.root) or source.stat().st_size>max_bytes or digest(source)!=checksum:
            raise ValueError('Invalid local object')
        shutil.copyfile(source,target)


def validate_snapshot(s,job):
    if s.get('schema')!=1 or s.get('job_id')!=job or not re.fullmatch(r'[a-f0-9-]{36}',job) or not re.fullmatch(r'\d+\.\d+\.\d+',s.get('version','')) or len(s['version'])>40 or type(s.get('sequence')) is not int or s['sequence']<1:
        raise ValueError('Invalid snapshot identity')
    files=s.get('files',[])
    if not files or len(files)>5000 or len({f['filename'].casefold() for f in files})!=len(files):
        raise ValueError('Empty or ambiguous originals')
    if not set(s['catalog']['required']).issubset(f['filename'] for f in files):
        raise ValueError('Initial collection is incomplete')
    for f in files:
        if not valid_name(f['filename']) or not HEX.fullmatch(f['sha256']) or f['key']!='originals/sha256/'+f['sha256']+'.png' or type(f['bytes']) is not int or not 0<f['bytes']<=20*1024**2:
            raise ValueError('Invalid original')
        if not f['pairs'] or any(not re.fullmatch(r'[A-Z0-9]{2,4}',p['type']) or not re.fullmatch(r'[A-Z]{3}|\*',p['airline']) for p in f['pairs']):
            raise ValueError('Invalid aircraft/airline mapping')
    return files

def fetch_and_verify_original(store, ink, f):
    path=ink/'liveries'/f['filename']
    store.get(f['key'],path,f['sha256'],f['bytes'])

    if path.stat().st_size!=f['bytes']:
        raise ValueError('Original size mismatch')

    with Image.open(path) as im:
        if im.format!='PNG' or im.width*im.height>16_000_000:
            raise ValueError('Invalid PNG')
        im.load()
        if not im.convert('RGBA').getchannel('A').getbbox():
            raise ValueError('Empty PNG')

    return f

def build_snapshot(s,store,work,conversion_cache=None):
    files=validate_snapshot(s,s['job_id'])
    work.mkdir(parents=True,exist_ok=False)
    bootstrap=work/'bootstrap.zip'; b=s['bootstrap']
    if not HEX.fullmatch(b['sha256']) or b['key']!='publisher-bootstrap-'+b['sha256']+'.zip' or b['bytes']>512*1024**2:
        raise ValueError('Invalid bootstrap')
    with phase('bootstrap'):
        store.get(b['key'],bootstrap,b['sha256'],b['bytes'])
        ink=work/'ink'; (ink/'liveries').mkdir(parents=True);(ink/'mappings').mkdir()
        with zipfile.ZipFile(bootstrap) as z:
            expected={'library.zip','catalog-digest.txt'}|{'mappings/'+n for n in ('aircraft_workbook.json','airlines_workbook.json','livery_api_overrides.json','regional_livery_assignments.json','skywest_livery_assignments.json')}
            if set(z.namelist())!=expected or len(z.infolist())!=len(expected) or sum(i.file_size for i in z.infolist())>512*1024**2 or z.read('catalog-digest.txt').decode()!=s['catalog']['digest']:
                raise ValueError('Bootstrap does not match main catalog')
            (work/'base.zip').write_bytes(z.read('library.zip'))
            for name in expected-{'library.zip','catalog-digest.txt'}:
                (ink/name).write_bytes(z.read(name))
    choices={(r['type'],r['airline']):r['selected'] for r in s['catalog']['choices']}
    selected={}
    with phase('original download + verification'):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures=[
                pool.submit(fetch_and_verify_original,store,ink,f)
                for f in files
            ]

            for i,future in enumerate(as_completed(futures),1):
                future.result()
                if i%100==0 or i==len(files):
                    print('Verified originals:',i,'/',len(files),flush=True)

    selected={}
    for f in files:
        for p in f['pairs']:
            key=(p['type'],p['airline'])
            if key in choices and f['filename']!=choices[key]:
                continue
            old=selected.get(key)
            if old and old['sha256']!=f['sha256']:
                raise ValueError('Ambiguous image for '+str(key))
            selected[key]=f

    pairs=defaultdict(list)
    for key,f in selected.items(): pairs[f['filename']].append(key)
    with phase('artwork.zip'):
        with zipfile.ZipFile(work/'artwork.zip','x',zipfile.ZIP_STORED) as z:
            for name in sorted(pairs): z.write(ink/'liveries'/name,name)
    with phase('conversion'):
        audit=convert(work/'artwork.zip',work/'base.zip',work/'converted',s['version'],{}, {},file_pairs=pairs,conversion_cache=conversion_cache)
        print('Conversion cache: hits=',audit['conversion_cache_hits'],'misses=',audit['conversion_cache_misses'],flush=True)
        if audit['unmapped'] or audit['conflicts']: raise ValueError('Incomplete conversion')
    with phase('normalize_zip'):
        normalize_zip(work/'converted/liveries.zip')
    with phase('Ink aliases'):
        existing={p.name.casefold() for p in (ink/'liveries').iterdir()}
        for (kind,airline),f in sorted(selected.items()):
            model=s['catalog']['ink_types'].get(kind)
            brand='Unknown' if airline=='*' else s['catalog']['ink_airlines'].get(airline)
            if not model or not brand: continue
            name=model+'_'+brand+'.png'
            if not valid_name(name): raise ValueError('Invalid Ink alias')
            if name.casefold() not in existing:
                shutil.copyfile(ink/'liveries'/f['filename'],ink/'liveries'/name);existing.add(name.casefold())
    draft=work/'release'
    with phase('device package build'):
        catalog=build(work/'converted/liveries.zip',draft,s['version'],s['sequence'],s['created_at'][:10],repository=REPO,ink_assets=ink)
        feed=prepare_downloads(draft,channel='stable')
        ink_download=draft/'downloads'/('ink-'+s['version']+'.zip')
        normalize_zip(ink_download)
        ink_entry=next(p for p in feed['products'] if p['id']=='ink')
        ink_entry.update(bytes=ink_download.stat().st_size,sha256=digest(ink_download))
        (draft/'downloads/device-updates.json').write_text(json.dumps(feed,indent=2),encoding='utf8')
        upload=work/'assets';upload.mkdir()
        for file in (draft/'downloads').iterdir(): shutil.copyfile(file,upload/file.name)
        bundle=draft/catalog['package']['file'];shutil.copyfile(bundle,upload/bundle.name)
    with phase('verify_packages'):
        verify_packages(upload,s,files)
    receipt=dict(schema=1,job_id=s['job_id'],snapshot_sha256=s['_snapshot_sha256'],version=s['version'],sequence=s['sequence'],products=['ink','mini_800','micro_360'],originals=len(files),hardware_validation='pending')
    (upload/'publication.json').write_text(json.dumps(receipt,sort_keys=True),encoding='utf8')
    with phase('SHA256SUMS'):
        (upload/'SHA256SUMS.txt').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in sorted(upload.iterdir())),encoding='ascii')
    return upload


def verify_packages(upload,s,originals):
    feed=json.loads((upload/'device-updates.json').read_text())
    if feed['channel']!='stable' or feed['version']!=s['version'] or {p['id'] for p in feed['products']}!={'ink','mini_800','micro_360'}:
        raise ValueError('Incomplete device feed')
    pairs={}
    for p in feed['products']:
        path=upload/p['file']
        if path.parent!=upload or path.stat().st_size!=p['bytes'] or digest(path)!=p['sha256']:
            raise ValueError('Device payload checksum mismatch')
        if p['id']=='ink':
            with zipfile.ZipFile(path) as z:
                if z.testzip(): raise ValueError('Invalid Ink ZIP')
                for f in originals:
                    if hashlib.sha256(z.read('assets/liveries/'+f['filename'])).hexdigest()!=f['sha256']: raise ValueError('Missing Ink original')
            continue
        with path.open('rb') as stream:
            h=json.loads(stream.readline());plan=[]
            if h['product']!=p['id'] or h['version']!=s['version'] or hashlib.sha256(h['manifest'].encode()).hexdigest()!=h['manifest_sha256']: raise ValueError('Invalid device header')
            for _ in range(h['files']):
                entry=json.loads(stream.readline());data=stream.read(entry['bytes'])
                if len(data)!=entry['bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']: raise ValueError('Invalid device file')
                plan.append(f"{entry['path']}\t{entry['bytes']}\t{entry['sha256']}\n")
                if entry['path'].endswith('index.jsonl'):
                    rows=[json.loads(line) for line in data.splitlines()]
                    pairs[p['id']]={(r['type'],r['airline']) for r in rows if r['airline']!='*'}
                elif entry['path'].endswith('.png'):
                    with Image.open(io.BytesIO(data)) as im: im.load()
            if stream.read() or hashlib.sha256(''.join(plan).encode()).hexdigest()!=h['plan_sha256']: raise ValueError('Invalid update plan')
    inventory=json.loads((upload/'reporting-inventory.json').read_text())
    if {(p['type'],p['airline']) for p in inventory['entries']}!=pairs['mini_800'] or pairs['mini_800']!=pairs['micro_360']:
        raise ValueError('Reporting inventory is not complete')


class GitHub(GitHubPublisher):
    def request(self,method,path,data=None):
        c=http.client.HTTPSConnection('api.github.com',timeout=90)
        try:
            c.request(method,'/repos/'+REPO+path,body=None if data is None else json.dumps(data),headers={'Authorization':'Bearer '+self.token,'User-Agent':'PlaneSlate-Livery-Publisher','Accept':'application/vnd.github+json','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})
            r=c.getresponse();raw=r.read(4*1024**2)
            if r.status==404 and method=='GET':return None
            if not 200<=r.status<300:raise ValueError('GitHub request failed: HTTP '+str(r.status))
            return json.loads(raw)
        finally:c.close()

    def inventory_sequence(self,release):
        asset=next((a for a in release['assets'] if a['name']=='reporting-inventory.json'),None)
        if not asset or not re.fullmatch(r'sha256:[a-f0-9]{64}',asset.get('digest','')) or not 0<asset['size']<=1048576:
            raise ValueError('Latest release has no verified reporting inventory')
        url=asset['browser_download_url']
        for _ in range(5):
            u=urllib.parse.urlsplit(url)
            allowed=(u.hostname=='github.com' and u.path.startswith('/'+REPO+'/releases/download/')) or u.hostname in ('release-assets.githubusercontent.com','objects.githubusercontent.com')
            if u.scheme!='https' or u.port not in (None,443) or u.username or u.password or not allowed:raise ValueError('Untrusted inventory URL')
            c=http.client.HTTPSConnection(u.hostname,timeout=30)
            try:
                c.request('GET',u.path+('?' +u.query if u.query else ''),headers={'User-Agent':'PlaneSlate-Livery-Publisher'})
                r=c.getresponse()
                if r.status in (301,302,303,307,308):
                    url=urllib.parse.urljoin(url,r.getheader('Location'));continue
                if r.status!=200:raise ValueError('Latest inventory unavailable')
                raw=r.read(1048577)
                if len(raw)!=asset['size'] or 'sha256:'+hashlib.sha256(raw).hexdigest()!=asset['digest']:raise ValueError('Latest inventory checksum mismatch')
                inventory=json.loads(raw)
                if inventory.get('schema')!=1 or inventory.get('state')!='published' or release['tag_name']!='liveries-'+inventory.get('version','') or type(inventory.get('sequence')) is not int:
                    raise ValueError('Invalid latest inventory')
                return inventory['sequence']
            finally:c.close()
        raise ValueError('Inventory redirect limit')


def publish(upload,s,github):
    tag='liveries-'+s['version'];marker='PlaneSlate snapshot '+s['job_id']+' '+s['_snapshot_sha256']
    release=github.request('GET','/releases/tags/'+tag)
    if release and marker not in release.get('body',''):
        raise ValueError('Version already exists; choose a new version in the admin page')
    latest=github.request('GET','/releases/latest')
    if latest and latest['tag_name']!=tag:
        def number(t):
            if not re.fullmatch(r'liveries-\d+\.\d+\.\d+',t):raise ValueError('Unknown latest version')
            return tuple(map(int,t[len('liveries-'):].split('.')))
        if number(latest['tag_name'])>=number(tag):raise ValueError('Version must increase')
        if s['sequence']<=github.inventory_sequence(latest):raise ValueError('Release sequence must increase; refresh the catalog and create a new job')
    if not release:
        release=github.request('POST','/releases',dict(tag_name=tag,target_commitish=os.environ.get('GITHUB_SHA','main'),name='PlaneSlate Liveries '+s['version'],draft=True,prerelease=False,body=marker+'\n\nPakete für PlaneSlate Ink, Mini und Micro. Vollständige Originale, Katalog und Prüfsummen automatisch geprüft. Geräteprüfung separat.'))
    files={p.name:p for p in upload.iterdir()}
    remote={a['name']:a for a in release.get('assets',[])}
    if remote.keys()-files.keys():raise ValueError('Unexpected asset in release draft')
    with phase('GitHub upload'):
        pending=[]
        for name,p in files.items():
            old=remote.get(name)
            if old:
                if old.get('digest')!='sha256:'+digest(p) or old['size']!=p.stat().st_size:raise ValueError('Existing release asset differs; never overwrite')
            elif release['draft']:pending.append(p)
            else:raise ValueError('Published release is incomplete')
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs={pool.submit(github.upload,release['id'],p):p for p in pending}
            for i,job in enumerate(as_completed(jobs),1):
                job.result();print('Uploaded:',i,'/',len(jobs),jobs[job].name,flush=True)
    with phase('GitHub verification'):
        verified=github.request('GET','/releases/'+str(release['id']))
        actual={a['name']:a for a in verified['assets']}
        if set(actual)!=set(files) or any(actual[n].get('digest')!='sha256:'+digest(p) or actual[n]['size']!=p.stat().st_size for n,p in files.items()):
            raise ValueError('GitHub verification failed; draft retained')
    if verified['draft']:
        verified=github.request('PATCH','/releases/'+str(release['id']),dict(draft=False,prerelease=False,make_latest='true'))
    if verified.get('draft') is not False or verified.get('prerelease') is not False:raise ValueError('Publication not confirmed')
    print('Published:',verified['html_url'],flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--local-store',type=Path)
    parser.add_argument('--output',type=Path,default=Path('livery-build'))
    parser.add_argument('--original-cache',type=Path)
    parser.add_argument('--conversion-cache',type=Path)
    args=parser.parse_args()
    if args.publish and args.local_store: raise ValueError('Local verification cannot publish')
    job=os.environ['JOB_ID'];key=os.environ['SNAPSHOT_KEY'];checksum=os.environ['SNAPSHOT_SHA256']
    if not re.fullmatch(r'[a-f0-9-]{36}',job) or key!='snapshots/'+job+'.json' or not HEX.fullmatch(checksum):raise ValueError('Invalid dispatch inputs')
    store=LocalR2(args.local_store) if args.local_store else R2()
    if args.original_cache:store=CachedOriginals(store,args.original_cache)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    snapshot_file=args.output.with_suffix('.snapshot.json')
    with phase('snapshot download'):
        store.get(key,snapshot_file,checksum,4*1024**2)
    snapshot=json.loads(snapshot_file.read_text(encoding='utf8'));validate_snapshot(snapshot,job)
    snapshot['_snapshot_sha256']=checksum
    assets=build_snapshot(snapshot,store,args.output,conversion_cache=args.conversion_cache)
    if isinstance(store,CachedOriginals):print('Original cache: hits=',store.hits,'misses=',store.misses,flush=True)
    print('All device packages verified:',assets,flush=True)
    if args.publish:
        publish(assets,snapshot,GitHub(os.environ['GITHUB_TOKEN']))


if __name__=='__main__':main()
