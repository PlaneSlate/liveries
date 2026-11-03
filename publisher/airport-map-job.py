"""Authenticated background runner; job inputs and outputs use the existing R2 store."""
import hashlib,hmac,json,os,re,sys,tempfile,urllib.request
from datetime import datetime,timezone
from pathlib import Path
from airport_maps import fetch_geometry,render,encoded,sha,VARIANTS,PALETTES
from publisher import R2

def put(store,key,data,content='application/json'):
 if not re.fullmatch('airport-maps/[a-zA-Z0-9_./-]+',key) or '..' in key:raise ValueError('Invalid map object path')
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');day=stamp[:8];scope=day+'/auto/s3/aws4_request';digest=sha(data);path='/'+store.bucket+'/'+key
 names='host;x-amz-content-sha256;x-amz-date';headers=f'host:{store.host}\nx-amz-content-sha256:{digest}\nx-amz-date:{stamp}\n'
 canonical='PUT\n'+path+'\n\n'+headers+'\n'+names+'\n'+digest;string='AWS4-HMAC-SHA256\n'+stamp+'\n'+scope+'\n'+sha(canonical.encode());secret=('AWS4'+os.environ['R2_SECRET_ACCESS_KEY']).encode()
 for part in (day,'auto','s3','aws4_request'):secret=hmac.new(secret,part.encode(),hashlib.sha256).digest()
 signature=hmac.new(secret,string.encode(),hashlib.sha256).hexdigest();authorization=f"AWS4-HMAC-SHA256 Credential={os.environ['R2_ACCESS_KEY_ID']}/{scope}, SignedHeaders={names}, Signature={signature}"
 request=urllib.request.Request('https://'+store.host+path,method='PUT',data=data,headers={'Authorization':authorization,'x-amz-date':stamp,'x-amz-content-sha256':digest,'Content-Type':content})
 with urllib.request.urlopen(request,timeout=90) as response:
  if response.status not in (200,201):raise ValueError('Airport map storage failed')
def run(store,job,work,save):
 if set(job)-{'schema','id','mode','airport','identity','version','options','source','created_at'} or job.get('schema')!=1 or job.get('mode') not in ('fetch','render'):raise ValueError('Invalid map job')
 airport=job['airport'];icao=airport['icao'];id=job['id']
 if not re.fullmatch('[A-Z0-9]{4}',icao) or not re.fullmatch('[a-f0-9-]{36}',id):raise ValueError('Invalid map job identity')
 prefix='airport-maps/jobs/'+id+'/'
 def asset(raw,extension):
  key=prefix+sha(raw)+extension;save(key,raw,'image/png' if extension=='.png' else 'application/json');return dict(key=key,sha256=sha(raw),bytes=len(raw))
 progress=lambda message:save(prefix+'progress.json',encoded(dict(message=message)),'application/json')
 options=job.get('options',{})
 if set(options)-{'bounds','padding','density','optional','osm_id','viewport','master'}:raise ValueError('Invalid map options')
 if job['mode']=='fetch':
  progress('Retrieving airport perimeter and OSM infrastructure')
  raw,geo=fetch_geometry(airport,bbox=options.get('bounds'),osm_id=options.get('osm_id'))
  source=asset(encoded(raw),'.json');geometry=asset(encoded(geo),'.geojson')
  return dict(schema=1,id=id,mode='fetch',identity=job['identity'],source=source,geometry=geometry,counts=geo['counts'],warnings=geo['warnings'],bounds=geo['bounds'],retrieved_at=geo['retrieved_at'],source_timestamp=geo['source_timestamp'],lookup=dict(bounds=options.get('bounds'),osm_id=options.get('osm_id')))
 source=job.get('source',{});ref=source['geometry']
 if not re.fullmatch(r'airport-maps/jobs/[a-f0-9-]{36}/[a-f0-9]{64}\.geojson',ref['key']) or not 0<ref['bytes']<=24*1024**2:raise ValueError('Invalid geometry reference')
 path=work/'geometry.geojson';store.get(ref['key'],path,ref['sha256'],ref['bytes']);geo=json.loads(path.read_bytes())
 if geo['airport_icao']!=icao:raise ValueError('Source airport mismatch')
 assets=[];variants=dict(VARIANTS)
 if options.get('master'):variants['master']=(2048,2048)
 viewport=options.get('viewport')
 if viewport:
  if not isinstance(viewport,list) or len(viewport)!=2 or any(type(v)is not int or not 256<=v<=2048 for v in viewport):raise ValueError('Invalid Plus viewport')
  variants['plus']=tuple(viewport)
 for variant,size in variants.items():
  for style in PALETTES:
   progress('Rendering '+variant+' / '+style)
   png,meta=render(geo,variant,style,size=size,padding=options.get('padding',.08),density=options.get('density','normal'),optional=options.get('optional',True),bbox=options.get('bounds'),version=job['version'])
   assets.append(dict(**asset(png,'.png'),metadata=meta))
 return dict(schema=1,id=id,mode='render',identity=job['identity'],version=job['version'],assets=assets,source=source,options=options,counts=geo['counts'],warnings=geo['warnings'],bounds=geo['bounds'],generated_at=datetime.now(timezone.utc).isoformat())
def main():
 id=os.environ['JOB_ID'];checksum=os.environ['SNAPSHOT_SHA256'];key=os.environ['SNAPSHOT_KEY']
 if not re.fullmatch('[a-f0-9-]{36}',id) or key!='airport-maps/jobs/'+id+'/input.json':raise ValueError('Invalid job input')
 store=R2()
 with tempfile.TemporaryDirectory() as directory:
  work=Path(directory);path=work/'input.json';store.get(key,path,checksum,32768);job=json.loads(path.read_bytes())
  if job['id']!=id:raise ValueError('Job mismatch')
  try:result=run(store,job,work,lambda key,raw,content:put(store,key,raw,content))
  except Exception as error:
   put(store,'airport-maps/jobs/'+id+'/result.json',encoded(dict(schema=1,id=id,error=str(error)[:240])))
   raise
  put(store,'airport-maps/jobs/'+id+'/result.json',encoded(result))
if __name__=='__main__':main()
