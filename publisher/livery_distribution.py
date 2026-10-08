"""Build device downloads and publish immutable, verified GitHub test releases."""
import hashlib
import json
import os
import re
import tempfile
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from sd_manager import Package
def atomic_json(path, value):
    temporary=path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding="utf8")
    temporary.replace(path)

REPOSITORY = 'PlaneSlate/liveries'


def line(value):
    return (json.dumps(value, separators=(',', ':'), ensure_ascii=True)+'\n').encode()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare_downloads(draft, *, channel="test"):
    if channel not in ("test", "stable"):
        raise ValueError("Invalid release channel")
    draft = Path(draft)
    catalog = json.loads((draft/'livery-releases.json').read_text())
    if catalog['state'] != 'draft' or not catalog['all_devices_included'] or len(catalog['products'])!=2 or {p['id'] for p in catalog['products']}!={'mini_800','micro_360'}:
        raise ValueError('Ein vollstaendiger Entwurf fuer Mini und Micro wird benoetigt.')
    version = catalog['version']
    target = draft/'downloads'
    if target.exists():
        raise ValueError('Downloads sind bereits erstellt.')
    target.mkdir()
    bundle = draft/catalog['package']['file']
    if digest(bundle) != catalog['package']['sha256']:
        raise ValueError('Paket-Pruefsumme stimmt nicht.')
    tag = 'liveries-'+version
    feed = dict(schema=1, version=version, sequence=catalog['sequence'], channel=channel, products=[])
    with zipfile.ZipFile(bundle) as archive:
        for product in catalog['products']:
            filename = f"{product['id']}-{version}.psu"
            with tempfile.TemporaryDirectory() as temp:
                source = Path(temp)/'library.zip'
                source.write_bytes(archive.read(product['payload']['path']))
                if digest(source)!=product['payload']['sha256']:
                    raise ValueError('Geraetepaket-Pruefsumme stimmt nicht.')
                package = Package(source)
                try:
                    with (target/filename).open('xb') as stream:
                        stream.write(line(dict(format='planeslate-update-v1', product=product['id'],
                            version=version, files=len(package.files), plan_sha256=package.plan,
                            manifest_sha256=hashlib.sha256(package.manifest_raw).hexdigest(),
                            manifest=package.manifest_raw.decode())))
                        for name,size,checksum in package.files:
                            stream.write(line(dict(path=name, bytes=size, sha256=checksum)))
                            stream.write(package.read(name,64*1024**2))
                finally:
                    package.close()
            format_name = 'planeslate-update-v1'
            file = target/filename
            feed['products'].append(dict(id=product['id'], format=format_name, file=filename,
                bytes=file.stat().st_size, sha256=digest(file),
                url=f'https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}'))
    # Complete exact-pair inventory, never just this release's additions.
    # GitHub's stable release gate decides when the server may activate this file.
    pairs = set()
    for product in ('mini_800', 'micro_360'):
        inventory = json.loads((draft/('inventory-'+product+'.json')).read_text())
        if inventory.get('version') != catalog['version']:
            raise ValueError('Inventory version mismatch')
        record = next(p['inventory'] for p in catalog['products'] if p['id'] == product)
        if digest(draft/record['file']) != record['sha256']:
            raise ValueError('Inventory checksum mismatch')
        for entry in inventory['entries']:
            if entry.get('asset_type','livery') != 'livery': continue
            kind, airline = entry['type'], entry['airline']
            if not re.fullmatch(r'[A-Z0-9]{2,4}', kind) or not re.fullmatch(r'[A-Z]{3}|\*', airline):
                raise ValueError('Invalid inventory pair')
            if airline != '*':
                pairs.add((kind, airline))
    if not pairs or len(pairs) > 10000:
        raise ValueError('Empty or oversized reporting inventory')
    atomic_json(target/'reporting-inventory.json', dict(schema=1, state='published',
        version=catalog['version'], sequence=catalog['sequence'],
        entries=[dict(type=kind, airline=airline) for kind,airline in sorted(pairs)]))
    atomic_json(target/'device-updates.json', feed)
    return feed


class GitHubPublisher:
    """Token is injected by the operator's environment, never accepted by the website."""
    def __init__(self, token=None):
        self.token = token or os.environ.get('PLANESLATE_GITHUB_TOKEN', '')
        if not self.token:
            raise ValueError('GitHub-Verbindung fehlt. Der Entwurf bleibt lokal erhalten.')

    def request(self, method, path, data):
        request = urllib.request.Request('https://api.github.com/repos/'+REPOSITORY+path,
            data=json.dumps(data).encode(), method=method,
            headers={'Authorization':'Bearer '+self.token, 'Accept':'application/vnd.github+json',
                     'Content-Type':'application/json', 'User-Agent':'PlaneSlate-Livery-Publisher'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def upload(self, release_id, file):
        # Release assets use GitHub's separate upload origin. Token never follows a redirect.
        import http.client
        connection = http.client.HTTPSConnection('uploads.github.com', timeout=120)
        try:
            path = '/repos/'+REPOSITORY+'/releases/'+str(release_id)+'/assets?name='+urllib.parse.quote(file.name)
            connection.putrequest('POST', path)
            for key,value in {'Authorization':'Bearer '+self.token,'User-Agent':'PlaneSlate-Livery-Publisher',
                              'Content-Type':'application/octet-stream','Content-Length':str(file.stat().st_size)}.items():
                connection.putheader(key,value)
            connection.endheaders()
            with file.open('rb') as source:
                while block := source.read(65536):
                    connection.send(block)
            response=connection.getresponse()
            result=response.read(65536)
            if response.status!=201:
                raise ValueError('GitHub-Upload fehlgeschlagen; der Release bleibt als Entwurf bestehen.')
            metadata=json.loads(result)
            if metadata.get('size')!=file.stat().st_size or metadata.get('digest')!='sha256:'+digest(file):
                raise ValueError('GitHub hat die erwartete Dateipruefsumme nicht bestaetigt.')
            return metadata
        finally:
            connection.close()


def publish_test(draft, store, publisher):
    """Publish first, then expose availability; any upload failure leaves reports open."""
    draft=Path(draft)
    catalog=json.loads((draft/'livery-releases.json').read_text())
    if catalog['state']!='draft' or not catalog['all_devices_included']:
        raise ValueError('Vollstaendiger Entwurf erforderlich.')
    if (draft/'publication-attempt.json').exists():
        raise ValueError('Es existiert bereits ein Veroeffentlichungsversuch. Den GitHub-Entwurf zuerst pruefen.')
    if not (draft/'downloads/device-updates.json').exists():
        prepare_downloads(draft)
    files=[draft/catalog['package']['file'], *sorted((draft/'downloads').iterdir())]
    if digest(files[0])!=catalog['package']['sha256']:
        raise ValueError('Paket wurde nach der Pruefung veraendert.')
    feed=json.loads((draft/'downloads/device-updates.json').read_text())
    for product in feed['products']:
        path=draft/'downloads'/product['file']
        if path.parent!=draft/'downloads' or digest(path)!=product['sha256'] or path.stat().st_size!=product['bytes']:
            raise ValueError('Download wurde nach der Pruefung veraendert.')
    release=publisher.request('POST','/releases',dict(tag_name=catalog['release_tag'],
        name='Livery test '+catalog['version'],draft=True,prerelease=True,
        body='Test release for PlaneSlate Ink, Mini and Micro. Hardware verification pending.'))
    atomic_json(draft/'publication-attempt.json',dict(id=release['id'],state='uploading'))
    for file in files:
        publisher.upload(release['id'],file)
    published=publisher.request('PATCH','/releases/'+str(release['id']),dict(draft=False,prerelease=True,make_latest='false'))
    if published.get('draft') is not False or published.get('prerelease') is not True:
        raise ValueError('GitHub hat die Test-Veroeffentlichung nicht bestaetigt.')
    # Test publications must NOT resolve requests for users on the stable channel.
    receipt=dict(state='test_published',version=catalog['version'],url=published['html_url'],
        published_at=datetime.now(timezone.utc).isoformat(),
        feed=f"https://github.com/{REPOSITORY}/releases/download/{catalog['release_tag']}/device-updates.json")
    atomic_json(draft/'publication-attempt.json',receipt)
    return receipt
