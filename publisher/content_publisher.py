"""Build both common device packages from one checksum-bound captured job.

Uses the existing image publisher and its validated caches. Default mode builds
only; --publish verifies immutable prior inventories and uploads one common
release. No live catalog queries or counter acknowledgment.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import zipfile

from publisher import (build_snapshot, validate_snapshot, CachedOriginals, LocalR2,
                       R2, digest, GitHub)
from catalog_package import encoded, sha, validate
from content_release import prepare, verify, validate_capture, PRODUCTS
from content_changes import validate_inventory

REPOSITORY='PlaneSlate/liveries'


def read_release_asset(release,name,limit):
    matches=[a for a in release['assets'] if a['name']==name]
    if len(matches)!=1:raise ValueError('Missing or duplicate common release asset')
    asset=matches[0]
    if (asset.get('state')!='uploaded' or type(asset.get('size')) is not int or not 0<asset['size']<=limit
            or not re.fullmatch(r'sha256:[a-f0-9]{64}',asset.get('digest',''))):
        raise ValueError('Unverified common release asset')
    url=asset['browser_download_url']
    expected='https://github.com/'+REPOSITORY+'/releases/download/'+release['tag_name']+'/'+name
    if url!=expected:raise ValueError('Untrusted common asset URL')
    for _ in range(5):
        parsed=urllib.parse.urlsplit(url)
        if (parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password
                or parsed.hostname not in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com')):
            raise ValueError('Untrusted common asset redirect')
        connection=http.client.HTTPSConnection(parsed.hostname,timeout=60)
        try:
            # Credentials stay on api.github.com; asset redirects receive none.
            connection.request('GET',parsed.path+('?' +parsed.query if parsed.query else ''),
                               headers={'User-Agent':'PlaneSlate-Content-Publisher','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                url=urllib.parse.urljoin(url,response.getheader('Location'));continue
            if response.status!=200:raise ValueError('Common release asset download failed')
            raw=response.read(asset['size']+1)
            if len(raw)!=asset['size'] or 'sha256:'+sha(raw)!=asset['digest']:
                raise ValueError('Common release asset checksum mismatch')
            return raw
        finally:connection.close()
    raise ValueError('Common release redirect limit')


class ContentGitHub(GitHub):
    def published_receipts(self):
        releases=self.request('GET','/releases?per_page=100')
        results=[]
        for release in releases:
            if not release.get('tag_name','').startswith('content-') or release['draft'] or release['prerelease']:continue
            receipt=json.loads(read_release_asset(release,'content-release.json',16384))
            if (receipt.get('schema')!=1 or receipt.get('state')!='verified' or receipt.get('component')!='content'
                    or receipt.get('repository')!=REPOSITORY or receipt.get('release_tag')!=release['tag_name']
                    or release['tag_name']!='content-'+receipt.get('version','')
                    or type(receipt.get('sequence')) is not int or receipt['sequence']<1):
                raise ValueError('Invalid published common receipt')
            validate_capture(receipt.get('capture'))
            marker='PlaneSlate common content\nJob: '+receipt['capture']['job_id']+'\nSnapshot SHA256: '+receipt['capture']['snapshot_sha256']
            if release.get('body')!=marker:raise ValueError('Common receipt belongs to a different job')
            results.append((release,receipt))
        if len({r['sequence'] for _,r in results})!=len(results):raise ValueError('Conflicting common release sequences')
        return sorted(results,key=lambda item:item[1]['sequence'],reverse=True)

    def latest_sequence(self):
        return max((r['sequence'] for _,r in self.published_receipts()),default=0)

    def baselines(self,version,sequence):
        results=[]
        for release,receipt in self.published_receipts():
            if receipt['sequence']>sequence or (receipt['sequence']==sequence and receipt['version']!=version):
                raise ValueError('A newer common release is already published')
            if receipt['version']==version:continue
            if len(results)>=16:break
            raw=read_release_asset(release,'content-inventory.json',64*1024**2)
            inventory=json.loads(raw);validate_inventory(inventory)
            record=receipt['inventory']
            if (record['file']!='content-inventory.json' or record['bytes']!=len(raw) or record['sha256']!=sha(raw)
                    or inventory['version']!=receipt['version'] or inventory['sequence']!=receipt['sequence']):
                raise ValueError('Baseline inventory differs from published receipt')
            results.append(inventory)
        return results


def publish_captured(output,snapshot,github,baselines=()):
    output=Path(output)
    receipt=verify_captured(output,snapshot,baselines)
    if github.latest_sequence()>snapshot['sequence']:raise ValueError('Refusing stale common publication')
    body='PlaneSlate common content\nJob: '+snapshot['job_id']+'\nSnapshot SHA256: '+snapshot['_snapshot_sha256']
    release=github.request('GET','/releases/tags/'+receipt['release_tag'])
    if release is None:
        release=github.request('POST','/releases',dict(tag_name=receipt['release_tag'],name='PlaneSlate Content '+snapshot['version'],
                              body=body,draft=True,prerelease=False,make_latest='false'))
    if release.get('body')!=body or release.get('prerelease') is not False:
        raise ValueError('Common release belongs to a different captured job')
    paths={p.name:p for p in output.iterdir()}
    def check_assets(current,complete):
        assets=current['assets']
        if len({a['name'] for a in assets})!=len(assets):raise ValueError('Duplicate common release assets')
        actual={a['name']:a for a in assets}
        if not set(actual).issubset(paths) or (complete and set(actual)!=set(paths)):
            raise ValueError('Incomplete or unexpected common release assets')
        for name,asset in actual.items():
            if (asset.get('state')!='uploaded' or asset['size']!=paths[name].stat().st_size
                    or asset.get('digest')!='sha256:'+digest(paths[name])):
                raise ValueError('Existing common release asset differs; never overwrite')
        return actual
    present=check_assets(release,not release['draft'])
    for name,path in paths.items():
        if name not in present:github.upload(release['id'],path)
    checked=github.request('GET','/releases/'+str(release['id']));check_assets(checked,True)
    # Reverify local content and remote sequence immediately before public visibility.
    verify_captured(output,snapshot,baselines)
    if github.latest_sequence()>snapshot['sequence']:raise ValueError('A newer common publication superseded this job')
    if checked['draft']:
        checked=github.request('PATCH','/releases/'+str(release['id']),dict(draft=False,prerelease=False,make_latest='false'))
    if checked.get('draft') is not False or checked.get('prerelease') is not False:
        raise ValueError('Common publication not confirmed')
    checked=github.request('GET','/releases/'+str(release['id']));check_assets(checked,True)
    if checked['draft'] or checked['prerelease']:raise ValueError('Common publication remains a draft')
    return receipt


def load_capture(raw,job_id,checksum):
    capture=dict(job_id=job_id,snapshot_sha256=checksum)
    validate_capture(capture)
    if len(raw)>4*1024**2 or sha(raw)!=checksum:
        raise ValueError('Common snapshot checksum or size mismatch')
    snapshot=json.loads(raw)
    validate_snapshot(snapshot,job_id)
    if snapshot.get('component')!='content':raise ValueError('A common captured job is required')
    device=snapshot.get('device_catalog')
    validate(device)
    chunks=snapshot.get('airport_chunks')
    if chunks is not None:
        if not isinstance(chunks,list) or len(chunks)>1200:raise ValueError('Invalid airport capture chunks')
        for i,chunk in enumerate(chunks):
            if (not isinstance(chunk,dict) or set(chunk)!={'key','bytes','sha256'}
                    or chunk['key']!=f'content-snapshots/{job_id}/airports-{i}.json'
                    or type(chunk['bytes']) is not int or not 2<=chunk['bytes']<=2*1024**2
                    or not isinstance(chunk['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',chunk['sha256'])):
                raise ValueError('Invalid airport capture reference')
    for name,field in [('catalog_revision','revision'),('assets_revision','assets_revision')]:
        if type(snapshot.get(name)) is not int or snapshot[name]<0 or snapshot[name]!=device.get(field):
            raise ValueError('Common captured revision mismatch')
    if not isinstance(snapshot.get('created_at'),str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z',snapshot['created_at']):
        raise ValueError('Invalid common capture timestamp')
    snapshot['_snapshot_sha256']=checksum
    return snapshot


def verify_captured(output,snapshot,baselines=()):
    receipt=verify(output,baselines)
    expected=dict(job_id=snapshot['job_id'],snapshot_sha256=snapshot['_snapshot_sha256'])
    if (receipt.get('capture')!=expected or receipt['version']!=snapshot['version']
            or receipt['sequence']!=snapshot['sequence'] or receipt['repository']!='PlaneSlate/liveries'
            or receipt['catalog_revision']!=snapshot['catalog_revision']
            or receipt['assets_revision']!=snapshot['assets_revision']
            or receipt['catalog_snapshot_sha256']!=sha(encoded(snapshot['device_catalog']))):
        raise ValueError('Common artifacts differ from captured job')
    return receipt


def image_alias_choices(snapshot):
    """Resolve spelling aliases only when the captured catalog has one exact name."""
    from copy import deepcopy
    from collections import defaultdict
    images=deepcopy(snapshot)
    catalog=images['catalog'];groups=defaultdict(list)
    explicit={(c['type'],c['airline']) for c in catalog.get('choices',[])}
    for file in images['files']:
        for pair in file['pairs']:
            if 'registration' not in pair and 'operator' not in pair:
                groups[(pair['type'],pair['airline'])].append(file['filename'])
    normalize=lambda name:name.replace('_',' ').casefold()
    for (type_code,airline),names in groups.items():
        if len(names)<2 or (type_code,airline) in explicit:continue
        canonical=catalog['ink_types'].get(type_code,'')+'_'+catalog['ink_airlines'].get(airline,'')+'.png'
        if names.count(canonical)==1 and all(normalize(name)==normalize(canonical) for name in names):
            catalog.setdefault('choices',[]).append({'type':type_code,'airline':airline,'selected':canonical})
    return images

def resolve_airport_chunks(snapshot,store):
    if 'airport_chunks' not in snapshot or 'airports' in snapshot['device_catalog']:return
    from airport_catalog import validate as validate_airports
    rows=[];total=0
    with tempfile.TemporaryDirectory() as directory:
        for i,chunk in enumerate(snapshot['airport_chunks']):
            target=Path(directory)/f'{i}.json'
            store.get(chunk['key'],target,chunk['sha256'],2*1024**2)
            raw=target.read_bytes();total+=len(raw)
            if len(raw)!=chunk['bytes'] or sha(raw)!=chunk['sha256'] or total>128*1024**2:raise ValueError('Airport capture checksum or size mismatch')
            part=json.loads(raw)
            if not isinstance(part,list) or len(part)>128 or len(rows)+len(part)>150000:raise ValueError('Invalid airport capture rows')
            rows.extend(part)
    snapshot['device_catalog']['airports']=validate_airports(rows)


def build_captured(snapshot,store,output,baselines=(),*,conversion_cache=None,logo_cache=None):
    resolve_airport_chunks(snapshot,store)
    output=Path(output)
    # Completed retries verify the same artifacts. Partial directories fail
    # closed; a fresh workflow attempt can rebuild using the same snapshot/cache.
    if output.exists():
        return verify_captured(output/'content',snapshot,baselines)
    output.mkdir(parents=True)
    airport_logos={};airport_logo_bytes=0
    for airport in snapshot['device_catalog'].get('airports',[]):
        logo=airport.get('logo')
        if not logo or logo['sha256'] in airport_logos:continue
        folder=output/'airport-originals';folder.mkdir(exist_ok=True)
        path=folder/(logo['sha256']+'.png');store.get(logo['key'],path,logo['sha256'],512*1024)
        airport_logo_bytes+=path.stat().st_size
        if airport_logo_bytes>64*1024**2:raise ValueError('Airport logo capture exceeds budget')
        airport_logos[logo['sha256']]=path
    images=output/'images'
    build_snapshot(image_alias_choices(snapshot),store,images,conversion_cache=conversion_cache,logo_cache=logo_cache)
    draft=images/'release'
    image_receipt=json.loads((draft/'livery-releases.json').read_bytes())
    if (image_receipt['version']!=snapshot['version'] or image_receipt['sequence']!=snapshot['sequence']
            or len(image_receipt['products'])!=2 or {p['id'] for p in image_receipt['products']}!=set(PRODUCTS)):
        raise ValueError('Image build identity mismatch')
    bundle=draft/image_receipt['package']['file']
    if bundle.parent!=draft or digest(bundle)!=image_receipt['package']['sha256']:
        raise ValueError('Image bundle checksum mismatch')
    with tempfile.TemporaryDirectory(dir=output) as directory,zipfile.ZipFile(bundle) as archive:
        libraries={}
        for product in image_receipt['products']:
            item=product['payload']
            if (not item['path'].startswith('devices/') or '..' in item['path']
                    or item['bytes']>512*1024**2):raise ValueError('Invalid image product payload')
            path=Path(directory)/(product['id']+'.zip')
            with archive.open(item['path']) as source,path.open('xb') as target:
                total=0
                while block:=source.read(65536):
                    total+=len(block)
                    if total>item['bytes']:raise ValueError('Image payload exceeds captured size')
                    target.write(block)
            if total!=item['bytes'] or digest(path)!=item['sha256']:
                raise ValueError('Image product checksum mismatch')
            libraries[product['id']]=path
        prepare(snapshot['device_catalog'],libraries,output/'content',snapshot['version'],snapshot['sequence'],
                baselines,'PlaneSlate/liveries',radar_sources=snapshot.get('radar_sources',[]),airport_logos=airport_logos,capture=dict(job_id=snapshot['job_id'],snapshot_sha256=snapshot['_snapshot_sha256']))
    return verify_captured(output/'content',snapshot,baselines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--local-store',type=Path)
    parser.add_argument('--output',type=Path,default=Path('content-build'))
    parser.add_argument('--original-cache',type=Path)
    parser.add_argument('--conversion-cache',type=Path)
    parser.add_argument('--logo-cache',type=Path)
    args=parser.parse_args()
    if args.publish and args.local_store:raise ValueError('Local verification cannot publish')
    job,key,checksum=(os.environ[name] for name in ('JOB_ID','SNAPSHOT_KEY','SNAPSHOT_SHA256'))
    if key!='content-snapshots/'+job+'.json':raise ValueError('Invalid common snapshot key')
    store=LocalR2(args.local_store) if args.local_store else R2()
    if args.original_cache:store=CachedOriginals(store,args.original_cache)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as directory:
        path=Path(directory)/'snapshot.json'
        store.get(key,path,checksum,4*1024**2)
        snapshot=load_capture(path.read_bytes(),job,checksum)
    github=ContentGitHub(os.environ['GITHUB_TOKEN']) if args.publish else None
    baselines=github.baselines(snapshot['version'],snapshot['sequence']) if github else ()
    build_captured(snapshot,store,args.output,baselines,conversion_cache=args.conversion_cache,logo_cache=args.logo_cache)
    print('Verified common Micro + Mini content:',args.output/'content')
    if github:publish_captured(args.output/'content',snapshot,github,baselines)


if __name__=='__main__':main()
