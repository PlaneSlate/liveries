"""Prepare a verified common content release and bounded device change preview.

No network, live database queries or publication acknowledgment. Image packs must
come from the same captured job as the supplied catalog snapshot; orchestration
must establish that provenance before calling this local preparation step.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import tempfile
import zipfile

from catalog_package import build as build_catalog, encoded, sha, validate, write_package
from content_changes import content_inventory, content_changes, validate_inventory
from content_package import Package, PRODUCTS, build as build_content, check_references

MAX_BASELINES = 16


def validate_summaries(entries,version,sequence):
    if not isinstance(entries,list) or len(entries)>MAX_BASELINES:
        raise ValueError('Invalid retained change summaries')
    versions,sequences=set(),set()
    for entry in entries:
        if (not isinstance(entry,dict) or set(entry)!={'from_version','from_sequence','inventory_sha256','changes'}
                or not isinstance(entry['from_version'],str) or len(entry['from_version'])>40
                or not re.fullmatch(r'\d+\.\d+\.\d+',entry['from_version']) or entry['from_version']==version
                or type(entry['from_sequence']) is not int or not 1<=entry['from_sequence']<sequence
                or entry['from_version'] in versions or entry['from_sequence'] in sequences
                or not isinstance(entry['inventory_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',entry['inventory_sha256'])
                or not isinstance(entry['changes'],dict) or set(entry['changes'])!={'registrations','airlines','logos','liveries','aircraft'}):
            raise ValueError('Invalid retained change summary identity')
        versions.add(entry['from_version']);sequences.add(entry['from_sequence'])
        for counts in entry['changes'].values():
            if (not isinstance(counts,dict) or set(counts)!={'new','changed','removed'}
                    or any(type(value) is not int or not 0<=value<=100000 for value in counts.values())
                    or counts['new']+counts['changed']>100000 or counts['removed']+counts['changed']>100000):
                raise ValueError('Invalid retained change counts')


def metadata(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(file=path.name, bytes=path.stat().st_size, sha256=digest)


def image_inventory(libraries):
    """Project exactly the distributed variants, ignoring paths and review notes."""
    images, logos = defaultdict(dict), defaultdict(dict)
    pairs = defaultdict(set)
    for product in PRODUCTS:
        package = Package(libraries[product])
        try:
            if package.kind != 'library':
                raise ValueError('Expected verified image library')
            layout = 'compact' if product == 'micro_360' else 'large'
            index = package.manifest['index']
            for raw in package.read(index['path'], 64*1024**2).splitlines():
                row = json.loads(raw)
                if row['layout'] != layout:
                    # Existing recovery packs can contain both layouts. Only the
                    # variant actually used by this product contributes to counts.
                    continue
                variant = {key:row[key] for key in ('sha256', 'bytes', 'width', 'height')}
                if row.get('asset_type', 'livery') == 'airline_logo':
                    identity = row['airline']
                    if product in logos[identity] and logos[identity][product] != variant:
                        raise ValueError('Conflicting logo variants')
                    logos[identity][product] = variant
                else:
                    name = row.get('source_filename')
                    if not name:
                        raise ValueError('Content preview requires original image filenames')
                    if product in images[name] and images[name][product] != variant:
                        raise ValueError('Conflicting image filename variants')
                    images[name][product] = variant
                    pairs[name].add((row['type'], row['airline'], row.get('asset_type', 'livery')))
        finally:
            package.close()
    # Product/layout agreement prevents advertising a complete shared update
    # whose images exist for only one of the two production products.
    for variants in [*images.values(), *logos.values()]:
        if set(variants) != set(PRODUCTS):
            raise ValueError('Image identity missing from one production product')
    return dict(files=[dict(filename=name, sha256=sha(encoded(variants)),
        pairs=[dict(type=kind, airline=airline, asset_type=asset_type) for kind, airline, asset_type in sorted(pairs[name])])
        for name, variants in sorted(images.items())],
        logos=[dict(icao=identity, variants=variants) for identity, variants in sorted(logos.items())])


def summaries(available, baselines):
    validate_inventory(available)
    entries, identities, versions = [], set(), set()
    for baseline in baselines:
        validate_inventory(baseline)
        identity = (baseline['version'], baseline['sequence'])
        if identity in identities or baseline['version'] in versions or baseline['sequence'] in {entry['from_sequence'] for entry in entries}:
            raise ValueError('Duplicate or conflicting retained baseline')
        identities.add(identity);versions.add(baseline['version'])
        if baseline['sequence'] >= available['sequence'] or baseline['version'] == available['version']:
            raise ValueError('Retained baseline must precede this release')
        comparison = content_changes(baseline, available)
        entries.append(dict(from_version=baseline['version'], from_sequence=baseline['sequence'],
            inventory_sha256=sha(encoded(baseline)), changes=comparison['changes']))
    entries.sort(key=lambda entry:entry['from_sequence'], reverse=True)
    entries=entries[:MAX_BASELINES];validate_summaries(entries,available['version'],available['sequence']);return entries


def prepare(snapshot, libraries, output, version, sequence, baselines=(), repository='PlaneSlate/planeslate', *, capture=None):
    if repository not in ('PlaneSlate/planeslate', 'PlaneSlate/liveries') or set(libraries) != set(PRODUCTS):
        raise ValueError('Invalid common release inputs')
    baselines=tuple(baselines)
    validate(snapshot)
    files = build_catalog(snapshot, version, sequence)
    inventory = content_inventory(snapshot, image_inventory(libraries), version, sequence)
    changes = summaries(inventory, baselines)
    first_install = content_changes(None, inventory)['changes']
    output = Path(output);output.mkdir(parents=True, exist_ok=False)
    catalog = output / ('planeslate-catalog-' + version + '.zip')
    write_package(files, catalog)
    products = []
    for product in PRODUCTS:
        path = output / (product + '-content-' + version + '.psu')
        result = build_content(libraries[product], catalog, path, product, version, sequence)
        products.append(dict(result, kind='content', file=path.name,
            url='https://github.com/'+repository+'/releases/download/content-'+version+'/'+path.name))
    (output/'content-inventory.json').write_bytes(encoded(inventory)+b'\n')
    feed = dict(schema=1, component='content', channel='stable', version=version, sequence=sequence,
        requires=dict(content_reader=1, catalog_reader=1), products=products,
        changes=changes, first_install=first_install)
    raw = encoded(feed)+b'\n'
    if len(raw) > 16384:
        raise ValueError('Common update feed exceeds device budget')
    (output/'content-updates.json').write_bytes(raw)
    descriptor = dict(schema=1, state='verified', component='content', version=version, sequence=sequence,
        catalog_revision=snapshot['revision'], assets_revision=snapshot['assets_revision'],
        catalog_snapshot_sha256=sha(encoded(snapshot)), image_packs={p:metadata(Path(libraries[p])) for p in PRODUCTS},
        catalog=metadata(catalog), inventory=metadata(output/'content-inventory.json'),
        feed=metadata(output/'content-updates.json'), products=products,
        release_tag='content-'+version, repository=repository)
    if capture is not None:
        validate_capture(capture)
        descriptor['capture']=capture
    (output/'content-release.json').write_bytes(encoded(descriptor)+b'\n')
    records = [metadata(path) for path in output.iterdir() if path.is_file()]
    (output/'SHA256SUMS.txt').write_text(''.join(row['sha256']+'  '+row['file']+'\n' for row in sorted(records,key=lambda row:row['file'])), encoding='utf-8')
    verify(output,baselines)
    return descriptor


def validate_capture(capture):
    if (not isinstance(capture,dict) or set(capture)!={'job_id','snapshot_sha256'}
            or not isinstance(capture['job_id'],str)
            or not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}',capture['job_id'])
            or not isinstance(capture['snapshot_sha256'],str)
            or not re.fullmatch('[a-f0-9]{64}',capture['snapshot_sha256'])):
        raise ValueError('Invalid common capture identity')


def extract_component(path, output, kind, product):
    """Reconstruct a recovery ZIP for independent existing package validation."""
    with path.open('rb') as source, zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        raw=source.readline(8193);header=json.loads(raw)
        if (len(raw)>8192 or header.get('format')!='planeslate-update-v1'
                or header.get('product')!=product or header.get('kind')!=kind
                or type(header.get('files')) is not int or not 1<=header['files']<=100001
                or not isinstance(header.get('manifest'),str)
                or len(header['manifest'].encode('utf-8'))>4096
                or sha(header['manifest'].encode('utf-8'))!=header.get('manifest_sha256')):
            raise ValueError('Invalid shared component header')
        archive.writestr(kind+'/manifest.json',header['manifest'].encode('utf-8'))
        plan=hashlib.sha256()
        for _ in range(header['files']):
            raw=source.readline(8193);item=json.loads(raw)
            if len(raw)>8192 or set(item)!={'path','bytes','sha256'} or type(item['bytes']) is not int or not 0<=item['bytes']<=64*1024**2:
                raise ValueError('Invalid shared file record')
            plan.update(f"{item['path']}\t{item['bytes']}\t{item['sha256']}\n".encode())
            digest=hashlib.sha256();left=item['bytes']
            with archive.open(item['path'],'w') as target:
                while left:
                    block=source.read(min(left,65536))
                    if not block:raise ValueError('Truncated shared file')
                    target.write(block);digest.update(block);left-=len(block)
            if digest.hexdigest()!=item['sha256']:raise ValueError('Shared file checksum mismatch')
        if source.read(1) or plan.hexdigest()!=header.get('plan_sha256'):
            raise ValueError('Invalid shared file plan')
    package=Package(output)
    try:
        if package.kind!=kind or package.version!=header.get('version'):
            raise ValueError('Shared manifest identity mismatch')
    finally:package.close()


def verify(output,baselines=None):
    """Verify stored artifacts independently, including counts from decoded packs.

    Historical baseline authenticity still belongs to immutable release retrieval
    in the publication orchestrator; its inventories are not embedded on devices.
    """
    output=Path(output)
    if (output/'content-release.json').stat().st_size>16384:
        raise ValueError('Common release receipt exceeds budget')
    descriptor=json.loads((output/'content-release.json').read_bytes())
    if 'capture' in descriptor:validate_capture(descriptor['capture'])
    if descriptor.get('schema')!=1 or descriptor.get('state')!='verified' or descriptor.get('component')!='content':
        raise ValueError('Invalid common release receipt')
    version,sequence=descriptor['version'],descriptor['sequence']
    repository=descriptor['repository']
    if repository not in ('PlaneSlate/planeslate','PlaneSlate/liveries') or descriptor['release_tag']!='content-'+version:
        raise ValueError('Untrusted common release')
    expected={'content-release.json','SHA256SUMS.txt'}
    records=[descriptor['catalog'],descriptor['inventory'],descriptor['feed'],*descriptor['products']]
    if [row['id'] for row in descriptor['products']]!=list(PRODUCTS):
        raise ValueError('Both common product artifacts are required')
    for record in records:
        name=record['file']
        if not isinstance(name,str) or Path(name).name!=name or '/' in name or '\\' in name or name in expected:
            raise ValueError('Unsafe or duplicate common release artifact')
        expected.add(name)
        if metadata(output/name)!={key:record[key] for key in ('file','bytes','sha256')}:
            raise ValueError('Common release artifact changed')
    if {path.name for path in output.iterdir()}!=expected:
        raise ValueError('Unexpected common release artifact')
    sums=''.join(row['sha256']+'  '+row['file']+'\n' for row in sorted([*records,metadata(output/'content-release.json')],key=lambda row:row['file']))
    if (output/'SHA256SUMS.txt').read_text(encoding='utf-8')!=sums:
        raise ValueError('Common checksum list differs')
    feed_raw=(output/descriptor['feed']['file']).read_bytes()
    if len(feed_raw)>16384:raise ValueError('Common feed exceeds budget')
    feed=json.loads(feed_raw)
    validate_summaries(feed['changes'],version,sequence)
    inventory=json.loads((output/descriptor['inventory']['file']).read_bytes());validate_inventory(inventory)
    if baselines is not None and feed['changes']!=summaries(inventory,baselines):
        raise ValueError('Common summaries differ from retained release inventories')
    if (inventory['version']!=version or inventory['sequence']!=sequence
            or any(feed.get(key)!=value for key,value in dict(schema=1,component='content',channel='stable',version=version,sequence=sequence,
                requires=dict(content_reader=1,catalog_reader=1),products=descriptor['products']).items())
            or feed['first_install']!=content_changes(None,inventory)['changes']):
        raise ValueError('Common feed or inventory identity differs')
    catalog=Package(output/descriptor['catalog']['file'])
    try:
        if catalog.kind!='catalog' or catalog.version!=version or catalog.manifest['sequence']!=sequence:
            raise ValueError('Common catalog identity differs')
        snapshot=dict(schema=1,component='catalog',revision=catalog.manifest['revision'],
            assets_revision=catalog.manifest['assets_revision'],assets_sequence=0)
        for table in ('aircraft','airlines','aliases','registrations'):
            meta=catalog.manifest['tables'][table]['data']
            rows=[json.loads(raw) for raw in catalog.read(meta['path'],64*1024**2).splitlines()]
            if table in ('aircraft','airlines'):
                for row in rows:row.pop('key')
            snapshot[table]=rows
        if (descriptor['catalog_revision']!=snapshot['revision'] or descriptor['assets_revision']!=snapshot['assets_revision']):
            raise ValueError('Captured common revisions differ')
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            libraries={}
            for record in descriptor['products']:
                product=record['id'];filename=product+'-content-'+version+'.psu'
                if (record['file']!=filename or record['kind']!='content' or record['format']!='planeslate-content-v1'
                        or record['url']!='https://github.com/'+repository+'/releases/download/content-'+version+'/'+filename):
                    raise ValueError('Invalid common product descriptor')
                with (output/filename).open('rb') as source:
                    raw=source.readline(2050);header=json.loads(raw)
                    if (len(raw)>2049 or header.get('format')!='planeslate-content-v1' or header.get('product')!=product
                            or header.get('version')!=version or header.get('sequence')!=sequence
                            or header.get('requires')!={'content_reader':1} or len(header.get('components',[]))!=2):
                        raise ValueError('Invalid common stream header')
                    for kind,part in zip(('library','catalog'),header['components']):
                        if part.get('kind')!=kind or type(part.get('bytes')) is not int or not 1<=part['bytes']<=1024**3:
                            raise ValueError('Invalid common component framing')
                        path=Path(temporary)/(product+'-'+kind+'.psu');digest=hashlib.sha256();left=part['bytes']
                        with path.open('xb') as target:
                            while left:
                                block=source.read(min(left,65536))
                                if not block:raise ValueError('Truncated common stream')
                                target.write(block);digest.update(block);left-=len(block)
                        if digest.hexdigest()!=part['sha256']:raise ValueError('Common component checksum mismatch')
                        package_path=path.with_suffix('.zip');extract_component(path,package_path,kind,product)
                        if kind=='library':libraries[product]=package_path
                        else:
                            parsed=Package(package_path)
                            try:
                                if parsed.manifest_raw!=catalog.manifest_raw or parsed.files!=catalog.files:
                                    raise ValueError('Common products contain different catalogs')
                            finally:parsed.close()
                    if source.read(1):raise ValueError('Trailing common stream data')
                library=Package(libraries[product])
                try:check_references(library,catalog,product)
                finally:library.close()
            actual=content_inventory(snapshot,image_inventory(libraries),version,sequence)
            if actual!=inventory:raise ValueError('Common counts inventory differs from distributed content')
    finally:catalog.close()
    return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    for product in PRODUCTS:
        parser.add_argument('--'+product.replace('_','-'), type=Path, required=True)
    parser.add_argument('--baseline', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', type=int, required=True)
    args = parser.parse_args()
    descriptor = prepare(json.loads(args.snapshot.read_text(encoding='utf-8-sig')),
        {product:getattr(args,product) for product in PRODUCTS}, args.output, args.version, args.sequence,
        [json.loads(path.read_text(encoding='utf-8')) for path in args.baseline])
    print('Prepared common content '+descriptor['version']+'; publication is separate.')
