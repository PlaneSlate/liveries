"""Build deterministic, bounded local SD catalogs from the authenticated snapshot.

No network, firmware build or administrative metadata is part of this format.
The existing SD transfer remains responsible for verified activation.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import zipfile

INDEX_BYTES = 256
MAX_ROW_BYTES = 8192
TABLES = ('aircraft', 'airlines', 'aliases', 'registrations')
AIRCRAFT_FIELDS = ('icao', 'iata_type', 'manufacturer', 'model', 'aircraft_type',
                   'engine_type', 'engine_count', 'wake_turbulence_category',
                   'planeslate_name', 'livery_name')
# GENERATED RADAR CATEGORIES
RADAR_SYMBOLS = ('airliner_standard', 'airliner_heavy', 'small_aircraft', 'business_jet', 'helicopter', 'glider', 'balloon', 'military')
# END RADAR CATEGORIES
CATEGORIES = ('special_livery', 'historic', 'research', 'rare', 'corporate', 'government')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def text(value, limit=1200):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in '\t\r\n' for c in value):
        raise ValueError('Invalid catalog text')
    return value


def exact(row, fields):
    if not isinstance(row, dict) or set(row) != set(fields):
        raise ValueError('Unknown or missing device fields; do not publish administrative metadata')


def airline_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z]{3}|named:[a-z0-9_-]{1,80}', value):
        raise ValueError('Invalid airline identity')
    return value


def validate(snapshot):
    exact(snapshot, ('schema', 'component', 'revision', 'assets_revision', 'assets_sequence', *TABLES) + (('airports',) if 'airports' in snapshot else ()))
    if 'airports' in snapshot:
        from airport_catalog import validate as validate_airports
        validate_airports(snapshot['airports'])
    if snapshot['schema'] != 1 or snapshot['component'] != 'catalog':
        raise ValueError('Unknown catalog snapshot')
    for field in ('revision', 'assets_revision', 'assets_sequence'):
        if type(snapshot[field]) is not int or not 0 <= snapshot[field] <= 0xffffffff:
            raise ValueError('Invalid component revision')
    result = {}
    for table in TABLES:
        source = snapshot[table]
        if not isinstance(source, list) or len(source) > 100000:
            raise ValueError('Invalid catalog table')
        result[table] = {}
        for row in source:
            if table == 'aircraft':
                exact(row, AIRCRAFT_FIELDS + (('radar_symbol',) if 'radar_symbol' in row else ()) + (('radar_shape',) if 'radar_shape' in row else ()))
                if row.get('radar_symbol', '') not in ('', *RADAR_SYMBOLS):
                    raise ValueError('Invalid radar symbol')
                shape = row.get('radar_shape')
                if shape is not None:
                    if (not isinstance(shape,list) or not 3 <= len(shape) <= 32 or
                            any(not isinstance(p,list) or len(p)!=2 or any(type(v) is not int or not -100<=v<=100 for v in p) for p in shape)):
                        raise ValueError('Invalid aircraft radar shape')
                for field,value in row.items():
                    if field != 'radar_shape':text(value)
                key = row['icao']
                if not re.fullmatch('[A-Z0-9]{2,4}', key):
                    raise ValueError('Invalid aircraft type')
            elif table == 'airlines':
                exact(row, ('id', 'icao', 'iata', 'airline_name', 'registered_in'))
                key = airline_id(row['id'])
                if (not row['airline_name'] or not re.fullmatch('[A-Z]{3}|', row['icao'])
                        or not re.fullmatch('[A-Z0-9]{2}|', row['iata'])
                        or (key.startswith('named:') and (row['icao'] or row['iata']))
                        or (not key.startswith('named:') and key != row['icao'])):
                    raise ValueError('Invalid airline codes or name')
                text(row['airline_name'], 120)
                text(row['registered_in'], 80)
            elif table == 'aliases':
                exact(row, ('key', 'ids'))
                key = row['key']
                if not isinstance(key, str) or not re.fullmatch('IATA:[A-Z0-9]{2}', key):
                    raise ValueError('Invalid alias')
                if (not isinstance(row['ids'], list) or not row['ids']
                        or len(row['ids']) > 100 or row['ids'] != sorted(set(row['ids']))):
                    raise ValueError('Invalid or ambiguous-order alias identities')
                for value in row['ids']:
                    airline_id(value)
            else:
                exact(row, ('key', 'registration', 'type', 'airline', 'operator', 'livery', 'special'))
                key = text(row['key'], 16)
                registration = text(row['registration'], 20)
                if (not re.fullmatch(r'(?:[A-Z0-9]{2,16}|[0-9]{2}\+[0-9]{2})', key)
                        or not re.fullmatch(r'(?:[A-Z0-9]+(?:-[A-Z0-9]+)*|[0-9]{2}\+[0-9]{2})', registration)
                        or registration.replace('-', '') != key):
                    raise ValueError('Registration identity mismatch')
                if not re.fullmatch('[A-Z0-9]{2,4}|', row['type']):
                    raise ValueError('Invalid registration aircraft type')
                if row['airline']:
                    airline_id(row['airline'])
                if not re.fullmatch('[A-Z]{3}|', row['operator']):
                    raise ValueError('Invalid operator code')
                livery = row['livery']
                if livery is not None:
                    exact(livery, ('filename', 'type'))
                    filename = text(livery['filename'], 180)
                    if (not filename.endswith('.png') or '/' in filename or '\\' in filename
                            or '..' in filename or filename != filename.strip()
                            or not row['type'] or livery['type'] != row['type']):
                        raise ValueError('Invalid livery reference or aircraft type')
                special = row['special']
                if special is not None:
                    exact(special, ('categories', 'name', 'short_name', 'description', 'short_description'))
                    if (not isinstance(special['categories'], list) or not special['categories']
                            or len(set(special['categories'])) != len(special['categories'])
                            or any(c not in CATEGORIES for c in special['categories'])
                            or not special['name']):
                        raise ValueError('Invalid active special profile')
                    for field, limit in (('name', 120), ('short_name', 40), ('description', 1200), ('short_description', 160)):
                        text(special[field], limit)
            if key in result[table]:
                raise ValueError('Duplicate catalog identity')
            result[table][key] = dict(row, key=key)
    if not result['aircraft'] or not result['airlines']:
        raise ValueError('Core catalog is empty')
    for row in result['aliases'].values():
        if any(value not in result['airlines'] for value in row['ids']):
            raise ValueError('Alias refers to missing airline')
    for row in result['registrations'].values():
        if ((row['airline'] and row['airline'] not in result['airlines'])
                or (row['type'] and row['type'] not in result['aircraft'])):
            raise ValueError('Registration refers to missing catalog identity')
    return result


def build(snapshot, version, sequence, places=None, airport_logos=None):
    if (not isinstance(version, str)
            or not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', version)
            or type(sequence) is not int or not 1 <= sequence <= 0xffffffff):
        raise ValueError('Invalid catalog version or sequence')
    tables = validate(snapshot)
    prefix = f'catalog/generations/{version}/'
    files, descriptors = {}, {}
    for name, rows in tables.items():
        data, index = bytearray(), bytearray()
        for key, row in sorted(rows.items()):
            raw = encoded(row) + b'\n'
            if len(raw) > MAX_ROW_BYTES:
                raise ValueError('Catalog row exceeds device memory budget')
            entry = encoded(dict(key=key, offset=len(data), bytes=len(raw), sha256=sha(raw)))
            if len(entry) >= INDEX_BYTES:
                raise ValueError('Catalog index row exceeds fixed record size')
            index.extend(entry + b' ' * (INDEX_BYTES - 1 - len(entry)) + b'\n')
            data.extend(raw)
        descriptors[name] = dict(entries=len(rows), record_bytes=INDEX_BYTES, max_row_bytes=MAX_ROW_BYTES)
        for suffix, value in (('index', bytes(index)), ('data', bytes(data))):
            path = prefix + name + ('.idx' if suffix == 'index' else '.jsonl')
            files[path] = value
            descriptors[name][suffix] = dict(path=path, bytes=len(value), sha256=sha(value))
    manifest = dict(schema=1, format='planeslate-catalog-v1', version=version, sequence=sequence,
                    revision=snapshot['revision'], assets_revision=snapshot['assets_revision'],
                    products=['micro_360', 'mini_800'], requires=dict(catalog_reader=1), tables=descriptors)
    # Optional extension: older packages with exactly eight table files remain valid.
    if places is None:
        source = Path(__file__).with_name('cities.json.gz')
        if not source.exists():
            source = Path(__file__).resolve().parents[1] / 'embedded/web/cities.json.gz'
        places = source.read_bytes()
    if places:
        path = prefix + 'cities.json.gz'
        files[path] = places
        manifest['places'] = dict(path=path, bytes=len(places), sha256=sha(places))
        manifest['requires']['catalog_reader'] = 2
    if 'airports' in snapshot:
        from airport_catalog import build as build_airports
        raw = build_airports(snapshot['airports'],airport_logos)
        path = prefix + 'airports.bin'
        files[path] = raw
        manifest['airports'] = dict(path=path, bytes=len(raw), sha256=sha(raw))
        manifest['requires']['catalog_reader'] = 3
    files['catalog/manifest.json'] = encoded(manifest)
    if len(files['catalog/manifest.json']) > 4096:
        raise ValueError('Catalog manifest exceeds device budget')
    return files


def write_package(files, output):
    output = Path(output)
    # Never silently replace a previously built immutable release artifact.
    with output.open('xb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)


def write_stream(files, output, product):
    """Reuse the device's bounded update stream and SD activation transaction."""
    if product not in ('micro_360', 'mini_800'):
        raise ValueError('Unsupported production product')
    manifest_raw = files['catalog/manifest.json']
    manifest = json.loads(manifest_raw)
    required = validate_package(manifest, lambda name, limit: files[name])
    if set(files) != set(required) | {'catalog/manifest.json'}:
        raise ValueError('Unexpected catalog package members')
    plan = ''.join(f'{name}\t{meta[0]}\t{meta[1]}\n' for name, meta in sorted(required.items())).encode()
    header = dict(format='planeslate-update-v1', kind='catalog', product=product,
                  version=manifest['version'], files=len(required), plan_sha256=sha(plan),
                  manifest_sha256=sha(manifest_raw), manifest=manifest_raw.decode('utf-8'))
    raw = encoded(header) + b'\n'
    if len(raw) > 8192:
        raise ValueError('Catalog stream header exceeds device limit')
    digest, size = hashlib.sha256(), 0
    with Path(output).open('xb') as stream:
        def emit(data):
            nonlocal size
            stream.write(data);digest.update(data);size += len(data)
        emit(raw)
        for name, meta in sorted(required.items()):
            emit(encoded(dict(path=name, bytes=meta[0], sha256=meta[1])) + b'\n')
            emit(files[name])
    return dict(bytes=size, sha256=digest.hexdigest())


def validate_package(manifest, read):
    """Verify the same bounded catalog accepted by the local firmware reader."""
    exact(manifest, ('schema', 'format', 'version', 'sequence', 'revision', 'assets_revision', 'products', 'requires', 'tables') + (('places',) if 'places' in manifest else ()) + (('airports',) if 'airports' in manifest else ()))
    if (manifest['schema'] != 1 or manifest['format'] != 'planeslate-catalog-v1'
            or manifest['products'] != ['micro_360', 'mini_800']
            or manifest['requires'] != {'catalog_reader': 3 if 'airports' in manifest else 2 if 'places' in manifest else 1}
            or set(manifest['tables']) != set(TABLES)):
        raise ValueError('Unsupported catalog manifest or compatibility')
    if not re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,46}[A-Za-z0-9_-])?', manifest['version']):
        raise ValueError('Invalid catalog version')
    for key in ('sequence', 'revision', 'assets_revision'):
        if type(manifest[key]) is not int or not (1 if key == 'sequence' else 0) <= manifest[key] <= 0xffffffff:
            raise ValueError('Invalid catalog sequence or revision')
    required, rows = {}, {}
    prefix = f"catalog/generations/{manifest['version']}/"
    if 'airports' in manifest:
        from airport_catalog import check as check_airports
        meta = manifest['airports']
        exact(meta, ('path','bytes','sha256'))
        if meta['path'] != prefix+'airports.bin' or type(meta['bytes']) is not int or not 16<=meta['bytes']<=128*1024**2 or not isinstance(meta['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',meta['sha256']):
            raise ValueError('Invalid airport catalog reference')
        raw=read(meta['path'],128*1024**2)
        if len(raw)!=meta['bytes'] or sha(raw)!=meta['sha256']:raise ValueError('Airport catalog checksum mismatch')
        check_airports(raw)
        required[meta['path']] = (meta['bytes'],meta['sha256'],None)
    if 'places' in manifest:
        meta = manifest['places']
        exact(meta, ('path', 'bytes', 'sha256'))
        if (meta['path'] != prefix+'cities.json.gz' or type(meta['bytes']) is not int
                or not 1 <= meta['bytes'] <= 4*1024**2 or not isinstance(meta['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', meta['sha256'])):
            raise ValueError('Invalid offline places reference')
        raw = read(meta['path'], 4*1024**2)
        if len(raw) != meta['bytes'] or sha(raw) != meta['sha256']:
            raise ValueError('Offline places checksum mismatch')
        # Bound expansion before parsing; reject zip bombs and malformed rows.
        import zlib
        decoder = zlib.decompressobj(31)
        expanded = decoder.decompress(raw, 32*1024**2+1)
        if len(expanded)>32*1024**2 or not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid offline places compression')
        cities = json.loads(expanded)
        if (not isinstance(cities,list) or not cities or any(not isinstance(row,list) or len(row)!=5
                or not isinstance(row[0],str) or not isinstance(row[1],str) or not isinstance(row[4],str)
                or any(type(row[i]) not in (float,int) for i in (2,3))
                or not -90<=row[2]<=90 or not -180<=row[3]<=180
                or len(encoded(row))>2048 for row in cities)):
            raise ValueError('Invalid offline place row')
        required[meta['path']] = (meta['bytes'], meta['sha256'], None)
    for name in TABLES:
        table = manifest['tables'][name]
        exact(table, ('entries', 'record_bytes', 'max_row_bytes', 'index', 'data'))
        count = table['entries']
        if (type(count) is not int or not 0 <= count <= 100000
                or table['record_bytes'] != INDEX_BYTES or table['max_row_bytes'] != MAX_ROW_BYTES):
            raise ValueError('Catalog exceeds device reader limits')
        content = {}
        for kind, ext in (('index', '.idx'), ('data', '.jsonl')):
            meta = table[kind]
            exact(meta, ('path', 'bytes', 'sha256'))
            if (meta['path'] != prefix + name + ext or type(meta['bytes']) is not int
                    or not 0 <= meta['bytes'] <= 64*1024**2
                    or not isinstance(meta['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', meta['sha256'])):
                raise ValueError('Invalid catalog file reference')
            raw = read(meta['path'], 64*1024**2)
            if len(raw) != meta['bytes'] or sha(raw) != meta['sha256']:
                raise ValueError('Catalog checksum mismatch')
            content[kind] = raw
            required[meta['path']] = (meta['bytes'], meta['sha256'], None)
        if len(content['index']) != count * INDEX_BYTES:
            raise ValueError('Invalid catalog index length')
        rows[name], previous, offset = [], '', 0
        for at in range(count):
            entry = json.loads(content['index'][at*INDEX_BYTES:(at+1)*INDEX_BYTES])
            exact(entry, ('key', 'offset', 'bytes', 'sha256'))
            if (not isinstance(entry['key'], str) or entry['key'] <= previous
                    or type(entry['offset']) is not int or entry['offset'] != offset
                    or type(entry['bytes']) is not int or not 1 <= entry['bytes'] <= MAX_ROW_BYTES):
                raise ValueError('Invalid, unsorted or overlapping catalog index')
            raw = content['data'][offset:offset+entry['bytes']]
            if len(raw) != entry['bytes'] or not raw.endswith(b'\n') or sha(raw) != entry['sha256']:
                raise ValueError('Invalid catalog row checksum')
            row = json.loads(raw)
            if row['key'] != entry['key']:
                raise ValueError('Catalog index identity mismatch')
            if name in ('aircraft', 'airlines'):
                row.pop('key')
            rows[name].append(row)
            previous, offset = entry['key'], offset + entry['bytes']
        if offset != len(content['data']):
            raise ValueError('Unindexed catalog data')
    validate(dict(schema=1, component='catalog', revision=manifest['revision'],
                  assets_revision=manifest['assets_revision'], assets_sequence=0, **rows))
    return required


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--stream-product', choices=('micro_360', 'mini_800'))
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    files = build(snapshot, args.version, args.sequence)
    if args.stream_product:
        write_stream(files, args.output, args.stream_product)
    else:
        write_package(files, args.output)
    print(f'Built {args.output.name}: {len(files)-1} verified catalog files; no image downloads.')
