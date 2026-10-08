"""Prepare an offline release draft from a validated embedded library snapshot.

Never contacts GitHub, serial ports or devices. Never publishes a latest pointer.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from datetime import date
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'embedded' / 'tools'))
from sd_manager import Package

PRODUCTS = {'mini_800': 'large', 'micro_360': 'compact'}


def artwork_paths(entries, prefix):
    """Deterministic 8.3 names; at most 64 images per directory.

    The index keeps the full SHA-256. Names use collision-free ordinals within
    this immutable generation, including when digests share a prefix.
    Two directory levels also bound fan-out for the maximum supported library.
    """
    hashes = sorted({entry['sha256'] for entry in entries})
    if len(hashes) > 100000:
        raise ValueError('Too many unique artwork files')
    return {sha: prefix + f'{i // 4096:08x}/{i // 64 % 64:08x}/{i % 64:08x}.png'
            for i, sha in enumerate(hashes)}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()


def write_json(path, value):
    path.write_bytes(encoded(value))


def archive_write(archive, name, data):
    info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def identity(entry):
    return (entry['type'], entry['airline'])


def changes(before, after):
    old = {identity(e): e['sha256'] for e in before}
    new = {identity(e): e['sha256'] for e in after}
    result = {}
    for label, keys in [('added', new.keys() - old.keys()),
                        ('updated', {k for k in old.keys() & new.keys() if old[k] != new[k]}),
                        ('removed', old.keys() - new.keys())]:
        result[label] = [dict(type=k[0], airline=k[1]) for k in sorted(keys)]
    return result


def load_previous(path):
    """Load only a complete locally verified release folder, never a loose index."""
    if path is None:
        return None, {}
    path = Path(path)
    catalog = json.loads((path / 'livery-releases.json').read_text())
    if catalog.get('schema') != 1 or not isinstance(catalog.get('sequence'), int):
        raise ValueError('Unsupported previous catalog')
    inventories = {}
    for product in catalog['products']:
        if product['id'] not in PRODUCTS or product.get('availability') != 'draft':
            continue
        metadata = product['inventory']
        filename = metadata['file']
        if Path(filename).name != filename or not re.fullmatch(r'[A-Za-z0-9_.-]+', filename):
            raise ValueError('Unsafe previous inventory path')
        target = path / filename
        if target.is_symlink() or digest(target) != metadata['sha256'] or target.stat().st_size != metadata['bytes']:
            raise ValueError('Previous inventory checksum mismatch')
        inventory = json.loads(target.read_text())
        if inventory['product'] != product['id'] or inventory['version'] != catalog['version']:
            raise ValueError('Previous inventory identity mismatch')
        inventories[product['id']] = inventory['entries']
    return catalog, inventories


def metadata(path):
    return dict(file=path.name, bytes=path.stat().st_size, sha256=digest(path))


def build(source, output, version, sequence, release_date, repository=None, previous=None):
    source, output = Path(source), Path(output).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', version):
        raise ValueError('Version must use 1–48 letters, digits, underscores, hyphens or internal dots')
    if type(sequence) is not int or sequence < 1:
        raise ValueError('Sequence must be a positive integer')
    if date.fromisoformat(release_date).isoformat() != release_date:
        raise ValueError('Date must be YYYY-MM-DD')
    if repository and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be owner/repository')
    if output.exists():
        raise FileExistsError('Output must be a new directory')
    old, inventories = load_previous(previous)
    if old and (sequence <= old['sequence'] or version == old['version']):
        raise ValueError('New release requires a new version and a larger sequence')
    source_hash = digest(source)
    package = Package(source)
    try:
        if package.kind != 'library':
            raise ValueError('A library package is required, not a map package')
        rows = [json.loads(line) for line in package.read(package.manifest['index']['path'], 64*1024**2).splitlines()]
        for product, layout in PRODUCTS.items():
            if not any(row['layout'] == layout for row in rows):
                raise ValueError(f'Missing artwork layout for {product}')
        output.parent.mkdir(parents=True, exist_ok=True)
        # Work in an isolated sibling directory; failures keep it for diagnosis.
        staging = Path(tempfile.mkdtemp(prefix='.livery-draft-', dir=output.parent))
        catalog = dict(schema=1, state='draft', version=version, sequence=sequence,
                       date=release_date, release_tag='liveries-'+version,
                       repository=repository, published_at=None,
                       source=dict(sha256=source_hash, version=package.version,
                                   mapping_version=package.manifest['mapping_version']),
                       products=[])
        changelog = dict(schema=1, version=version, previous_version=old['version'] if old else None,
                         comparison='previous_snapshot' if old else 'initial_snapshot', products={})
        prefix = f'library/generations/{version}/'
        for product, layout in PRODUCTS.items():
            selected = sorted((e for e in rows if e['layout'] == layout), key=identity)
            paths = artwork_paths(selected, prefix)
            index = []
            assets = {}
            for entry in selected:
                name = paths[entry['sha256']]
                assets[name] = entry['path']
                index.append(dict(entry, path=name))
            index_data = b''.join((json.dumps(e, sort_keys=True, separators=(',', ':'))+'\n').encode() for e in index)
            manifest = dict(schema=1, version=version, mapping_version=package.manifest['mapping_version'],
                            index=dict(path=prefix+'index.jsonl', sha256=hashlib.sha256(index_data).hexdigest(),
                                       bytes=len(index_data), entries=len(index)))
            archive_path = staging / f'planeslate-liveries-{product}-{version}.zip'
            with zipfile.ZipFile(archive_path, 'x') as archive:
                for name, original in sorted(assets.items()):
                    archive_write(archive, name, package.read(original, 512*1024))
                archive_write(archive, manifest['index']['path'], index_data)
                archive_write(archive, 'library/manifest.json', encoded(manifest))
            verified = Package(archive_path)
            verified.close()
            inventory_path = staging / f'inventory-{product}.json'
            inventory = [dict(type=e['type'], airline=e['airline'], sha256=e['sha256']) for e in index]
            write_json(inventory_path, dict(schema=1, version=version, product=product, entries=inventory))
            asset = metadata(archive_path)
            # Planned URLs are deliberately separate from available downloads.
            asset['download_url'] = None
            asset['planned_download_url'] = (f'https://github.com/{repository}/releases/download/'
                                             f'liveries-{version}/{archive_path.name}') if repository else None
            catalog['products'].append(dict(id=product, availability='draft',
                compatibility=dict(format='planeslate-sd-library', schema=1, layout=layout,
                                   hardware_validation='pending'),
                entries=len(index), unique_images=len(assets), package=asset,
                inventory=metadata(inventory_path)))
            changelog['products'][product] = changes(inventories.get(product, []), inventory)
        # One public bundle for the two supported devices.
        bundle_path = staging/f'planeslate-liveries-{version}.zip'
        payloads = []
        internal_archives = []
        with zipfile.ZipFile(bundle_path, 'x') as bundle:
            for product in catalog['products'][:2]:
                asset = product.pop('package')
                internal = staging/asset['file']
                name = 'devices/'+asset['file']
                archive_write(bundle, name, internal.read_bytes())
                product['payload'] = dict(path=name, bytes=asset['bytes'], sha256=asset['sha256'])
                payloads.append(dict(product=product['id'], **product['payload']))
                internal_archives.append(internal)
            archive_write(bundle, 'bundle.json', encoded(dict(schema=1, format='planeslate-livery-bundle',
                version=version, embedded=payloads)))
        with zipfile.ZipFile(bundle_path) as bundle:
            if bundle.testzip() is not None:
                raise ValueError('Universal bundle failed ZIP verification')
        for internal in internal_archives:
            internal.unlink()
        asset = metadata(bundle_path)
        asset['download_url'] = None
        asset['planned_download_url'] = (f'https://github.com/{repository}/releases/download/liveries-{version}/{bundle_path.name}') if repository else None
        catalog['package'] = asset
        catalog['format'] = 'planeslate-livery-bundle'
        catalog['installer_support'] = 'pending'
        catalog['all_devices_included'] = {p['id'] for p in catalog['products']} == set(PRODUCTS)
        write_json(staging/'changes.json', changelog)
        catalog['changes'] = metadata(staging/'changes.json')
        if digest(source) != source_hash:
            raise ValueError('Source changed during build; retry with a stable snapshot')
        write_json(staging/'livery-releases.json', catalog)
        files = sorted(staging.iterdir(), key=lambda p: p.name)
        (staging/'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in files), encoding='utf-8')
        staging.rename(output)
        return catalog
    finally:
        package.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Validated library ZIP snapshot')
    parser.add_argument('output', type=Path, help='New local draft directory')
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--date', required=True)
    parser.add_argument('--repository', help='Optional real GitHub owner/repository; does not upload')
    parser.add_argument('--previous', type=Path, help='Previous complete draft folder for change comparison')
    args = parser.parse_args()
    result = build(args.source, args.output, args.version, args.sequence, args.date, args.repository, args.previous)
    print(json.dumps(dict(state=result['state'], version=result['version'], products=[p['id'] for p in result['products'] if p['availability']=='draft'])))
