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
import shutil
import tempfile
import zipfile

from catalog_package import build as build_catalog, encoded, sha, validate, write_package
from content_changes import content_inventory, content_changes, validate_inventory
from content_package import Package, PRODUCTS, build as build_content, check_references

MAX_BASELINES = 16
SD_PRODUCTS = (*PRODUCTS, 'plus_1280')


def sd_activation(version, sequence, manifests):
    payload = encoded(dict(schema=1, version=version, sequence=sequence, components={
        kind: dict(base=sha(raw), manifest=raw.decode('utf-8'))
        for kind, raw in manifests.items()
    }))
    if len(payload) > 16384:
        raise ValueError('SD activation exceeds device limit')
    header = f'PS2\n1\n{len(payload)}\n'.encode()
    return header + sha(header + payload).encode() + b'\n' + payload


def sd_sources(library, catalog):
    return {name: (pack, digest) for pack in (library, catalog)
            for name, _, digest in pack.files}


def write_sd_card(library, catalog, output, version, sequence):
    manifests = {pack.kind: pack.manifest_raw for pack in (library, catalog)}
    activation = sd_activation(version, sequence, manifests)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name, (pack, _) in sd_sources(library, catalog).items():
            with pack.archive.open(name) as source, archive.open(name, 'w') as target:
                shutil.copyfileobj(source, target)
        for kind, raw in manifests.items():
            archive.writestr(zipfile.ZipInfo(f'{kind}/manifest.json'), raw)
        for slot in ('a', 'b'):
            archive.writestr(zipfile.ZipInfo(f'usb-content.{slot}'), activation)


def verify_sd_card(path, library, catalog, version, sequence):
    manifests = {pack.kind: pack.manifest_raw for pack in (library, catalog)}
    activation = sd_activation(version, sequence, manifests)
    records = {f'{kind}/manifest.json': sha(raw) for kind, raw in manifests.items()}
    records.update({f'usb-content.{slot}': sha(activation) for slot in ('a', 'b')})
    records.update({name: digest for name, (_, digest) in sd_sources(library, catalog).items()})
    with zipfile.ZipFile(path) as archive:
        if sorted(archive.namelist()) != sorted(records):
            raise ValueError('Incorrect SD card archive file list')
        for name, digest in records.items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('SD card archive differs from USB package: ' + name)


def build_sd_download(source, product, output, *, variant=None):
    # Use the same content and validation as the USB installer.
    from sd_manager import ContentPackage

    if product not in SD_PRODUCTS:
        raise ValueError('Unsupported SD download product')
    package = ContentPackage(source)
    try:
        if package.product != ('micro_360' if product == 'micro_360' else 'mini_800'):
            raise ValueError('SD download product does not match the source')
        if package.header.get('airport_variant') != variant:
            raise ValueError('SD download airport maps do not match the source')
        folder = Path(package.temporary.name)
        library = Package(folder / 'library.zip')
        try:
            catalog = Package(folder / 'catalog.zip')
            try:
                write_sd_card(library, catalog, output, package.version, package.sequence)
                verify_sd_card(output, library, catalog, package.version, package.sequence)
            finally:
                catalog.close()
        finally:
            library.close()
    finally:
        package.close()


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
                    if product!='mini_800' or row['type'] not in ('TAIL','LOGO') or row['layout']!='large':
                        raise ValueError('Airline logos require the large Plus library')
                    identity=row['airline']
                    variant_key=product+'-'+row['type'].lower()
                    if variant_key in logos[identity]:raise ValueError('Duplicate airline logo identity')
                    logos[identity][variant_key]=variant
                else:
                    name = row.get('source_filename')
                    if not name:
                        # Old bootstrap artwork predates source-filename metadata.
                        # Its validated mapping is a stable opaque inventory identity;
                        # it must still count, without inventing an original filename.
                        name='@legacy-'+sha(encoded([row['type'],row['airline'],row.get('asset_type','livery')]))+'.png'
                    if product in images[name] and images[name][product] != variant:
                        raise ValueError('Conflicting image filename variants')
                    images[name][product] = variant
                    pairs[name].add((row['type'], row['airline'], row.get('asset_type', 'livery')))
        finally:
            package.close()
    # Product/layout agreement prevents advertising a complete shared update
    # whose images exist for only one of the two production products.
    for variants in images.values():
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


def prepare(snapshot, libraries, output, version, sequence, baselines=(), repository='PlaneSlate/planeslate', *, capture=None, radar_sources=(), airport_logos=None,airport_map_images=None,airport_map_sources=None,airport_map_baselines=None):
    if repository not in ('PlaneSlate/planeslate', 'PlaneSlate/liveries') or set(libraries) != set(PRODUCTS):
        raise ValueError('Invalid common release inputs')
    baselines=tuple(baselines)
    validate(snapshot)
    files = build_catalog(snapshot, version, sequence,airport_logos=airport_logos)
    inventory = content_inventory(snapshot, image_inventory(libraries), version, sequence)
    changes = summaries(inventory, baselines)
    first_install = content_changes(None, inventory)['changes']
    output = Path(output);output.mkdir(parents=True, exist_ok=False)
    catalog = output / ('planeslate-catalog-' + version + '.zip')
    write_package(files, catalog)
    catalogs={};extra_assets=[]
    if any(a.get('maps') for a in snapshot.get('airports',[])):
        for variant in ('mini','plus'):
            package=output/('planeslate-catalog-'+variant+'-'+version+'.zip')
            device_files=build_catalog(snapshot,version,sequence,airport_logos=airport_logos,airport_map_images=airport_map_images,airport_map_variant=variant)
            write_package(device_files,package);catalogs[variant]=metadata(package)
    if any(a.get('maps') and a['maps'].get('kind')!='overlay' for a in snapshot.get('airports',[])):
        sources=output/('airport-maps-source-'+version+'.zip')
        if not airport_map_sources:raise ValueError('Airport map source archive required')
        with zipfile.ZipFile(sources,'x',zipfile.ZIP_DEFLATED) as archive:
            for name,raw in sorted(airport_map_sources.items()):
                if not re.fullmatch('[A-Z0-9]{4}\\.geojson',name):raise ValueError('Invalid airport source name')
                info=zipfile.ZipInfo(name,(2026,11,3,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,raw)
            archive.writestr('LICENSE.txt','© OpenStreetMap contributors. Geographic source data: ODbL-1.0. https://www.openstreetmap.org/copyright\n')
        extra_assets.append(metadata(sources))
    products = []
    for product in PRODUCTS:
        path = output / (product + '-content-' + version + '.psu')
        device_catalog=output/catalogs['mini']['file'] if product=='mini_800' and catalogs else catalog
        result = build_content(libraries[product], device_catalog, path, product, version, sequence)
        products.append(dict(result, kind='content', file=path.name,
            url='https://github.com/'+repository+'/releases/download/content-'+version+'/'+path.name))
    (output/'content-inventory.json').write_bytes(encoded(inventory)+b'\n')
    plus=output/('plus_1280-content-'+version+'.psu')
    build_content(libraries['mini_800'],output/catalogs['plus']['file'] if catalogs else catalog,plus,'mini_800',version,sequence)
    plus_asset=dict(metadata(plus),url='https://github.com/'+repository+'/releases/download/content-'+version+'/'+plus.name,format='planeslate-content-v1')
    if catalogs:plus_asset['airport_variant']='plus'
    feed = dict(schema=1, component='content', channel='stable', version=version, sequence=sequence,
        requires=dict(content_reader=1, catalog_reader=4 if catalogs else json.loads(files['catalog/manifest.json'])['requires']['catalog_reader']), products=products,
        changes=changes, first_install=first_install)
    feed['device_assets']={'plus_1280':plus_asset}
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
    descriptor['device_assets']={'plus_1280':plus_asset}
    if catalogs:descriptor['airport_map_catalogs']=catalogs
    current_maps={a['icao']:sha(encoded(a['maps'])) for a in snapshot.get('airports',[]) if a.get('maps')}
    if current_maps or airport_map_baselines:
        previous=airport_map_baselines or {};comparisons={}
        for baseline,old in previous.items():
            if not isinstance(old,dict) or any(not re.fullmatch('[A-Z0-9]{4}',k) or not re.fullmatch('[a-f0-9]{64}',v) for k,v in old.items()):raise ValueError('Invalid prior airport map inventory')
            comparisons[baseline]=dict(new=len(current_maps.keys()-old.keys()),removed=len(old.keys()-current_maps.keys()),changed=sum(current_maps[k]!=old[k] for k in current_maps.keys()&old.keys()),unchanged=sum(current_maps[k]==old[k] for k in current_maps.keys()&old.keys()))
        maps_inventory=output/'airport-map-inventory.json';maps_inventory.write_bytes(encoded(dict(schema=1,version=version,sequence=sequence,airports=current_maps)))
        changes_path=output/'airport-map-changes.json';changes_path.write_bytes(encoded(dict(schema=1,version=version,baselines=comparisons)))
        descriptor['airport_map_inventory']=metadata(maps_inventory);extra_assets.extend([metadata(maps_inventory),metadata(changes_path)])
    if extra_assets:descriptor['airport_map_assets']=extra_assets
    if any(row.get('radar_shape') for row in snapshot['aircraft']):
        source=Path(__file__).with_name('aircraft-shapes-source.zip')
        if not source.exists():source=Path(__file__).resolve().parents[1]/'deploy/liveries/aircraft-shapes-source.zip'
        with zipfile.ZipFile(source) as z:
            shapes=json.loads(z.read('aircraft-radar-shapes.json'))
            historical=json.loads(z.read('historical-radar-shapes.json')) if 'historical-radar-shapes.json' in z.namelist() else {}
        permitted=[*shapes.values(),*historical.values()]
        uploads=[]
        if not isinstance(radar_sources,(list,tuple)) or len(radar_sources)>1000:
            raise ValueError('Invalid radar upload sources')
        seen=set()
        for item in radar_sources:
            if (not isinstance(item,dict) or set(item)!={'id','name','points','svg','source_note'}
                    or not isinstance(item['svg'],str) or len(item['svg'])>256*1024
                    or not isinstance(item['name'],str) or len(item['name'])>100
                    or not isinstance(item['source_note'],str) or len(item['source_note'])>500):
                raise ValueError('Invalid radar upload metadata')
            bound=json.dumps({'svg':item['svg'],'points':item['points']},ensure_ascii=False,separators=(',',':')).encode('utf-8')
            identity='upload:'+sha(bound)
            if item['id']!=identity or identity in seen:raise ValueError('Radar upload checksum mismatch')
            seen.add(identity);permitted.append(item['points']);uploads.append(item)
        if any(row.get('radar_shape') and row['radar_shape'] not in permitted for row in snapshot['aircraft']):
            raise ValueError('Radar artwork sources differ from captured geometry')
        destination=output/'aircraft-shapes-source.zip';shutil.copyfile(source,destination)
        if uploads:
            with zipfile.ZipFile(destination,'a',zipfile.ZIP_DEFLATED) as archive:
                for item in sorted(uploads,key=lambda r:r['id']):
                    info=zipfile.ZipInfo('uploads/'+item['id'].split(':')[1]+'.svg',(2026,10,15,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                    archive.writestr(info,item['svg'].encode('utf-8'))
                info=zipfile.ZipInfo('uploads/manifest.json',(2026,10,15,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(info,json.dumps(sorted(uploads,key=lambda r:r['id']),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
        descriptor['artwork_source']=metadata(destination)
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
    map_catalogs=descriptor.get('airport_map_catalogs',{})
    if map_catalogs:
        if set(map_catalogs)!={'mini','plus'}:raise ValueError('Both airport map catalogs required')
        records.extend(map_catalogs.values())
    records.extend(descriptor.get('airport_map_assets',[]))
    device_assets=descriptor.get('device_assets',{})
    if device_assets:
        if not isinstance(device_assets,dict) or set(device_assets)!={'plus_1280'}:raise ValueError('Invalid supplemental device products')
        records.extend(device_assets.values())
    if 'artwork_source' in descriptor:
        if descriptor['artwork_source'].get('file')!='aircraft-shapes-source.zip':raise ValueError('Invalid artwork source name')
        records.append(descriptor['artwork_source'])
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
    if device_assets and feed.get('device_assets')!=device_assets:raise ValueError('Supplemental device feed differs from receipt')
    if map_catalogs and 'plus_1280' not in feed.get('device_assets',{}):raise ValueError('Plus airport content missing')
    validate_summaries(feed['changes'],version,sequence)
    inventory=json.loads((output/descriptor['inventory']['file']).read_bytes());validate_inventory(inventory)
    if baselines is not None and feed['changes']!=summaries(inventory,baselines):
        raise ValueError('Common summaries differ from retained release inventories')
    if (inventory['version']!=version or inventory['sequence']!=sequence
            or any(feed.get(key)!=value for key,value in dict(schema=1,component='content',channel='stable',version=version,sequence=sequence,
                products=descriptor['products']).items())
            or feed['first_install']!=content_changes(None,inventory)['changes']):
        raise ValueError('Common feed or inventory identity differs')
    catalog=Package(output/descriptor['catalog']['file'])
    try:
        if catalog.kind!='catalog' or catalog.version!=version or catalog.manifest['sequence']!=sequence:
            raise ValueError('Common catalog identity differs')
        if feed.get('requires')!={'content_reader':1,'catalog_reader':4 if map_catalogs else catalog.manifest['requires']['catalog_reader']}:
            raise ValueError('Common catalog reader requirement differs')
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
            product_records=list(descriptor['products'])
            if 'plus_1280' in feed.get('device_assets',{}):
                extra=feed['device_assets']['plus_1280']
                product_records.append(dict(extra,id='plus_1280',kind='content'))
            for record in product_records:
                product=record['id'];filename=product+'-content-'+version+'.psu'
                wire_product='mini_800' if product=='plus_1280' else product
                if (record['file']!=filename or record['kind']!='content' or record['format']!='planeslate-content-v1'
                        or record['url']!='https://github.com/'+repository+'/releases/download/content-'+version+'/'+filename):
                    raise ValueError('Invalid common product descriptor')
                with (output/filename).open('rb') as source:
                    raw=source.readline(2050);header=json.loads(raw)
                    expected_variant=('plus' if product=='plus_1280' else 'mini') if map_catalogs and product!='micro_360' else None
                    if header.get('airport_variant')!=expected_variant:raise ValueError('Wrong display variant in common content header')
                    if (len(raw)>2049 or header.get('format')!='planeslate-content-v1' or header.get('product')!=wire_product
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
                        package_path=path.with_suffix('.zip');extract_component(path,package_path,kind,wire_product)
                        if kind=='library':libraries[product]=package_path
                        else:
                            parsed=Package(package_path)
                            expected_catalog=Package(output/map_catalogs['plus' if product=='plus_1280' else 'mini']['file']) if product!='micro_360' and map_catalogs else catalog
                            try:
                                if parsed.manifest_raw!=expected_catalog.manifest_raw or parsed.files!=expected_catalog.files:
                                    raise ValueError('Common products contain different catalogs')
                            finally:
                                parsed.close()
                                if expected_catalog is not catalog:expected_catalog.close()
                    if source.read(1):raise ValueError('Trailing common stream data')
                library=Package(libraries[product])
                try:check_references(library,catalog,wire_product)
                finally:library.close()
            actual=content_inventory(snapshot,image_inventory(libraries),version,sequence)
            if actual!=inventory:raise ValueError('Common counts inventory differs from distributed content')
            if map_catalogs:
                for variant,record in map_catalogs.items():
                    device=Package(output/record['file'])
                    try:
                        if device.version!=version or device.manifest['tables']!=catalog.manifest['tables'] or device.manifest['requires']!={'catalog_reader':4}:raise ValueError('Device airport catalog differs from common core')
                        from airport_catalog import check as check_airports
                        meta=device.manifest['airports'];rows=check_airports(device.read(meta['path'],128*1024**2))
                        if any(a.get('maps') and a['maps']['variant']!=variant for a in rows):raise ValueError('Wrong airport map device variant')
                    finally:device.close()
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
