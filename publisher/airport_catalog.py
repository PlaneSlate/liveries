"""Bounded, latitude-sorted airport index and individually hashed device rows."""
import hashlib
import json
import math
import re
import struct

TYPES = ('large_airport','medium_airport','small_airport','heliport','seaplane_base','balloonport','closed_airport')
FIELDS = ('id','name','city','country','country_name','icao','iata','latitude','longitude','elevation_m','type','passengers','passengers_year','runways')
EXTRA_FIELDS = ('ident','gates','scheduled_service','description','description_de','description_en','logo')
ROW_LIMIT=32768
RECORD = struct.Struct('<IiiIIBB2x32s8x')
HEADER = struct.Struct('<4sIII')

def encode(row):
    return json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()

def validate(rows,stored=False):
    if not isinstance(rows,list) or len(rows)>150000:
        raise ValueError('Invalid airport catalog')
    ids=set()
    for a in rows:
        if not isinstance(a,dict) or not set(FIELDS)<=set(a) or set(a)-set(FIELDS)-set(EXTRA_FIELDS) or not re.fullmatch(r'oa:[1-9][0-9]{0,8}',a['id']) or a['id'] in ids:
            raise ValueError('Invalid/duplicate airport identity or device fields')
        ids.add(a['id'])
        for key,limit in (('ident',32),('description',2000),('description_de',2000),('description_en',2000)):
            v=a.get(key,'')
            if not isinstance(v,str) or len(v.encode())>limit or any(ord(c)<32 and c not in '\r\n\t' for c in v):raise ValueError('Invalid airport detail text')
        if a.get('gates') is not None and (type(a['gates']) is not int or not 0<=a['gates']<=100000):raise ValueError('Invalid gate count')
        if a.get('scheduled_service') is not None and type(a['scheduled_service']) is not bool:raise ValueError('Invalid scheduled service')
        logo=a.get('logo')
        if logo is not None:
            fields=('offset','bytes','sha256','width','height') if stored else ('key','sha256')
            if not isinstance(logo,dict) or set(logo)!=set(fields) or not isinstance(logo['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',logo['sha256']):raise ValueError('Invalid airport logo')
            if not stored and logo['key']!=f"admin/airports/logos/{int(a['id'][3:])}/{logo['sha256']}.png":raise ValueError('Invalid airport logo source')
            if stored and (any(type(logo[k]) is not int for k in ('offset','bytes','width','height')) or not 57<=logo['bytes']<=512*1024 or not 1<=logo['width']<=1024 or not 1<=logo['height']<=512):raise ValueError('Invalid stored airport logo')
        for key in ('name','city','country','country_name','icao','iata'):
            if not isinstance(a[key],str) or len(a[key].encode())>800 or any(ord(c)<32 for c in a[key]):
                raise ValueError('Invalid airport text')
        if not a['name'] or not re.fullmatch('[A-Z]{2}',a['country']) or not re.fullmatch('[A-Z0-9]{4}|',a['icao']) or not re.fullmatch('[A-Z0-9]{3}|',a['iata']) or a['type'] not in TYPES:
            raise ValueError('Invalid airport codes/type')
        for key,lo,hi in (('latitude',-90,90),('longitude',-180,180),('elevation_m',-1000,10000)):
            v=a[key]
            if v is None and key=='elevation_m':continue
            if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Invalid airport coordinate/elevation')
        if a['passengers'] is not None and (type(a['passengers']) is not int or not 0<=a['passengers']<=2**53-1 or type(a['passengers_year']) is not int or not 1900<=a['passengers_year']<=2200):
            raise ValueError('Passenger count requires a valid year')
        if a['passengers_year'] is not None and (type(a['passengers_year']) is not int or not 1900<=a['passengers_year']<=2200):raise ValueError('Invalid passenger year')
        if not isinstance(a['runways'],list) or len(a['runways'])>64:raise ValueError('Too many airport facilities')
        for r in a['runways']:
            if set(r)!=set(('ends','length_m','width_m','surface','closed','lighted')) or not isinstance(r['ends'],list) or len(r['ends'])!=2 or any(not isinstance(e,str) or len(e)>12 for e in r['ends']) or not isinstance(r['surface'],str) or len(r['surface'])>80 or type(r['closed']) is not bool or type(r['lighted']) is not bool:
                raise ValueError('Invalid airport facility')
            for key in ('length_m','width_m'):
                if r[key] is not None and (type(r[key]) not in (int,float) or not math.isfinite(r[key]) or not 0<=r[key]<=20000):raise ValueError('Invalid facility dimensions')
        if len(encode(a))>ROW_LIMIT:raise ValueError('Airport row exceeds device budget')
    return rows

def build(rows,logos=None):
    rows=validate(rows)
    ordered=sorted(rows,key=lambda a:(round(a['latitude']*1e6),a['id']))
    start=HEADER.size+RECORD.size*len(rows)
    index=bytearray();data=bytearray();images=bytearray();metadata={}
    for a in ordered:
        logo=a.get('logo')
        if not logo or logo['sha256'] in metadata:continue
        raw=(logos or {}).get(logo['sha256'])
        if hasattr(raw,'read_bytes'):raw=raw.read_bytes()
        if not isinstance(raw,bytes) or not 57<=len(raw)<=512*1024 or hashlib.sha256(raw).hexdigest()!=logo['sha256']:raise ValueError('Missing or corrupt airport logo payload')
        if raw[:8]!=b'\x89PNG\r\n\x1a\n' or raw[12:16]!=b'IHDR':raise ValueError('Airport logo is not PNG')
        width,height=struct.unpack_from('>II',raw,16)
        if not 1<=width<=1024 or not 1<=height<=512:raise ValueError('Invalid airport logo size')
        metadata[logo['sha256']]=dict(offset=start+len(images),bytes=len(raw),sha256=logo['sha256'],width=width,height=height)
        images+=raw
        if len(images)>64*1024**2:raise ValueError('Airport logo payload exceeds catalog budget')
    for a in ordered:
        stored=dict(a)
        if a.get('logo'):stored['logo']=metadata[a['logo']['sha256']]
        raw=encode(stored)
        if len(raw)>ROW_LIMIT:raise ValueError('Airport row exceeds device budget')
        index+=RECORD.pack(int(a['id'][3:]),round(a['latitude']*1e6),round(a['longitude']*1e6),start+len(images)+len(data),len(raw),TYPES.index(a['type']),bool(a['iata']),hashlib.sha256(raw).digest())
        data+=raw
    if start+len(images)+len(data)>128*1024**2:raise ValueError('Airport catalog exceeds package budget')
    return HEADER.pack(b'PSA2' if images else b'PSA1',len(rows),RECORD.size,start)+index+images+data

def check(raw):
    if len(raw)<HEADER.size:raise ValueError('Truncated airport header')
    magic,count,size,start=HEADER.unpack_from(raw)
    if magic not in (b'PSA1',b'PSA2') or count>150000 or size!=RECORD.size or start!=HEADER.size+count*size or start>len(raw):raise ValueError('Invalid airport index')
    first=RECORD.unpack_from(raw,HEADER.size)[3] if count else start
    if first<start or first>len(raw) or (magic==b'PSA1' and first!=start):raise ValueError('Invalid airport data boundary')
    rows=[];offset=first;previous=-90000001;images={}
    for i in range(count):
        ident,lat,lon,at,n,kind,iata,digest=RECORD.unpack_from(raw,HEADER.size+i*size)
        if lat<previous or at!=offset or not 1<=n<=ROW_LIMIT or at+n>len(raw) or kind>=len(TYPES) or iata>1:raise ValueError('Invalid airport record')
        row_raw=raw[at:at+n]
        if hashlib.sha256(row_raw).digest()!=digest:raise ValueError('Airport row checksum mismatch')
        a=json.loads(row_raw)
        validate([a],stored=True)
        logo=a.get('logo')
        if logo:
            if magic!=b'PSA2':raise ValueError('Unexpected airport logo')
            lo,length=logo.get('offset'),logo.get('bytes')
            if type(lo) is not int or type(length) is not int or lo<start or length<57 or lo+length>first:raise ValueError('Invalid airport logo range')
            image=raw[lo:lo+length]
            if hashlib.sha256(image).hexdigest()!=logo.get('sha256') or image[:8]!=b'\x89PNG\r\n\x1a\n' or image[12:16]!=b'IHDR' or tuple(struct.unpack_from('>II',image,16))!=(logo.get('width'),logo.get('height')):raise ValueError('Corrupt airport logo')
            images[lo]=length
        if a['id']!='oa:'+str(ident) or round(a['latitude']*1e6)!=lat or round(a['longitude']*1e6)!=lon or TYPES[kind]!=a['type'] or bool(a['iata'])!=bool(iata):raise ValueError('Airport index identity mismatch')
        rows.append(a);previous=lat;offset+=n
    if offset!=len(raw):raise ValueError('Airport trailing bytes')
    cursor=start
    for lo,length in sorted(images.items()):
        if lo!=cursor:raise ValueError('Unreferenced or overlapping logo payload')
        cursor+=length
    if cursor!=first:raise ValueError('Unreferenced logo bytes')
    validate(rows,stored=True)
    return rows
