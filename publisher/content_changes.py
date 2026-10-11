"""Device-relative change counts for verified shared content publications.

Compare complete inventories, not Admin pending counters or summed release
counts. Inventories contain opaque identity hashes and device-content hashes;
source/review evidence and registration strings are not exported here.
"""
import re
from catalog_package import encoded, sha, validate

CATEGORIES = ('registrations', 'airlines', 'logos', 'liveries', 'aircraft')


def content_inventory(catalog, assets, version, sequence):
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version) or len(version)>40:
        raise ValueError('Invalid content version')
    if type(sequence) is not int or not 1<=sequence<=0xffffffff:
        raise ValueError('Invalid content sequence')
    tables=validate(catalog)
    items={category:{} for category in CATEGORIES}

    def add(category, identity, value):
        key=sha(encoded(identity))
        if key in items[category]:
            raise ValueError('Duplicate content identity')
        items[category][key]=sha(encoded(value))

    for table in ('registrations', 'aircraft'):
        for key,row in tables[table].items():
            add(table,key,row)
    aliases={key:[] for key in tables['airlines']}
    for row in tables['aliases'].values():
        for identity in row['ids']:
            aliases[identity].append(row['key'])
    for key,row in tables['airlines'].items():
        add('airlines',key,dict(row=row,aliases=sorted(aliases[key])))
    # Inputs must be the exact verified, publishable image snapshot, not live DB rows.
    for image in assets['files']:
        name=image['filename']
        if not isinstance(name,str) or not name.endswith('.png') or '..' in name or any(c in name for c in '/\\'):
            raise ValueError('Invalid image identity')
        digest=image['sha256']
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
            raise ValueError('Invalid image digest')
        pairs=sorted(image['pairs'],key=lambda p:encoded(p))
        add('liveries',name,dict(sha256=digest,pairs=pairs))
    for logo in assets['logos']:
        identity=logo['icao']
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Z]{3}|named:[a-z0-9_-]{1,80}',identity):
            raise ValueError('Invalid logo identity')
        variants={}
        for name,variant in logo['variants'].items():
            digest=variant['sha256']
            if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
                raise ValueError('Invalid logo digest')
            variants[name]={field:variant[field] for field in ('sha256','bytes','width','height')}
        if not variants:
            raise ValueError('Missing logo variants')
        add('logos',identity,variants)
    return dict(schema=1,version=version,sequence=sequence,items=items)


def validate_inventory(inventory):
    if not isinstance(inventory,dict) or set(inventory)!= {'schema','version','sequence','items'} or inventory['schema']!=1:
        raise ValueError('Invalid content inventory')
    if not isinstance(inventory['version'],str) or not re.fullmatch(r'\d+\.\d+\.\d+',inventory['version']) or len(inventory['version'])>40:
        raise ValueError('Invalid inventory version')
    if type(inventory['sequence']) is not int or not 1<=inventory['sequence']<=0xffffffff or not isinstance(inventory['items'],dict) or set(inventory['items'])!=set(CATEGORIES):
        raise ValueError('Invalid inventory sequence/categories')
    for rows in inventory['items'].values():
        if not isinstance(rows,dict) or len(rows)>100000 or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',k) or not re.fullmatch('[a-f0-9]{64}',v) for k,v in rows.items()):
            raise ValueError('Invalid inventory entries')


def content_changes(installed, available):
    validate_inventory(available)
    if installed is not None:
        validate_inventory(installed)
        if installed['sequence']>available['sequence']:
            raise ValueError('Refusing older content')
        if installed['sequence']==available['sequence'] and installed!=available:
            raise ValueError('Immutable content sequence differs')
    changes={}
    for category in CATEGORIES:
        previous=installed['items'][category] if installed else {}
        current=available['items'][category]
        changes[category]=dict(new=len(current.keys()-previous.keys()),
            changed=sum(current[k]!=previous[k] for k in current.keys() & previous.keys()),
            removed=len(previous.keys()-current.keys()))
    return dict(schema=1,from_version=installed['version'] if installed else None,
        to_version=available['version'],changes=changes)
