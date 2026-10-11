"""Build one immutable Micro/Mini content stream from two verified local packs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'embedded' / 'tools'))
from sd_manager import Package

PRODUCTS = ('micro_360', 'mini_800')


def line(value):
    return (json.dumps(value, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def component_stream(package, product, path):
    with path.open('xb') as stream:
        stream.write(line(dict(format='planeslate-update-v1', kind=package.kind, product=product,
            version=package.version, files=len(package.files), plan_sha256=package.plan,
            manifest_sha256=hashlib.sha256(package.manifest_raw).hexdigest(),
            manifest=package.manifest_raw.decode('utf-8'))))
        for name, size, digest in package.files:
            stream.write(line(dict(path=name, bytes=size, sha256=digest)))
            stream.write(package.read(name, 64 * 1024**2))
    return dict(kind=package.kind, bytes=path.stat().st_size, sha256=checksum(path))


def check_references(library, catalog, product):
    """Explicit registration overrides must be present in this product's image pack.

    Normal automatic missing liveries are allowed; they are reported by devices.
    """
    layout = 'compact' if product == 'micro_360' else 'large'
    index = library.manifest['index']
    names = {(row['type'], row.get('source_filename')) for raw in
             library.read(index['path'], 64 * 1024**2).splitlines()
             if (row := json.loads(raw))['layout'] == layout
             and row.get('asset_type', 'livery') in ('livery', 'registration_livery')}
    table = catalog.manifest['tables']['registrations']['data']
    for raw in catalog.read(table['path'], 64 * 1024**2).splitlines():
        registration = json.loads(raw)
        override = registration.get('livery')
        if override and (override['type'], override['filename']) not in names:
            raise ValueError('Registration livery is absent from the shared product pack')


def build(library_path, catalog_path, output, product, version, sequence):
    if (product not in PRODUCTS or not isinstance(version, str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', version) or len(version) > 40
            or type(sequence) is not int or not 1 <= sequence <= 0xffffffff):
        raise ValueError('Invalid content release identity')
    output = Path(output)
    library, catalog = Package(library_path), None
    try:
        catalog = Package(catalog_path)
        if library.kind != 'library' or catalog.kind != 'catalog':
            raise ValueError('A library pack and a catalog pack are required')
        check_references(library, catalog, product)
        with tempfile.TemporaryDirectory(dir=output.parent) as temp:
            paths = [Path(temp) / 'library.psu', Path(temp) / 'catalog.psu']
            parts = [component_stream(package, product, path) for package, path in zip((library, catalog), paths)]
            header = line(dict(format='planeslate-content-v1', product=product, version=version,
                sequence=sequence, requires=dict(content_reader=1), components=parts))
            total = len(header) + sum(part['bytes'] for part in parts)
            if len(header) > 2049 or total > 1024**3:
                raise ValueError('Content exceeds device stream limits')
            # Exclusive creation protects immutable artifacts. Never remove an existing file.
            with output.open('xb') as stream:
                try:
                    stream.write(header)
                    for path in paths:
                        with path.open('rb') as source:
                            while block := source.read(65536):
                                stream.write(block)
                except BaseException:
                    stream.close()
                    output.unlink()
                    raise
        if output.stat().st_size != total:
            raise OSError('Shared content size differs after writing')
        return dict(format='planeslate-content-v1', id=product, bytes=total, sha256=checksum(output))
    finally:
        library.close()
        if catalog is not None:
            catalog.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--product', choices=PRODUCTS, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--sequence', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.library, args.catalog, args.output, args.product, args.version, args.sequence)))
