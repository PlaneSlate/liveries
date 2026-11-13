"""Approved airport diagram assets captured with the airport catalog, never fetched by firmware."""
import io,json,math,re
from PIL import Image

def reference(ref,extension):
 return isinstance(ref,dict) and type(ref.get('bytes')) is int and 0<ref['bytes']<=24*1024**2 and isinstance(ref.get('sha256'),str) and re.fullmatch('[a-f0-9]{64}',ref['sha256']) and isinstance(ref.get('key'),str) and ref.get('key')==ref.get('key','').strip() and re.fullmatch(r'airport-maps/jobs/[a-f0-9-]{36}/'+ref['sha256']+re.escape(extension),ref.get('key',''))

def validate_maps(maps,icao,stored=False):
 if maps is None:return
 if not isinstance(maps,dict) or type(maps.get('version')) is not int or maps['version']<1 or not isinstance(maps.get('assets'),list):raise ValueError('Invalid airport map manifest')
 overlay=maps.get('kind')=='overlay'
 if overlay:
  provenance=maps.get('provenance')
  if not isinstance(provenance,dict) or set(provenance)!={'source','license'} or any(not isinstance(v,str) or not v.strip() or len(v)>300 for v in provenance.values()):raise ValueError('Airport overlay requires documented provenance')
 seen=set()
 if stored:
  fields={'version','variant','assets'}|({'kind','provenance'} if overlay else set())
  if set(maps)!=fields or maps['variant'] not in ('mini','plus') or len(maps['assets'])!=(1 if overlay else 2):raise ValueError('Invalid local airport maps')
 elif len(maps['assets']) not in ((2,) if overlay else (6,8)):raise ValueError('Incomplete approved airport map set')
 for asset in maps['assets']:
  if not isinstance(asset,dict):raise ValueError('Invalid airport map asset')
  meta=asset if stored else asset.get('metadata',{})
  variant=meta.get('variant');style=meta.get('style');identity=(variant,style)
  if identity in seen or variant not in (('mini','plus') if overlay else ('thumbnail','mini','plus','master')) or style not in (('diagram_overlay',) if overlay else ('planeslate_dark','planeslate_light')):raise ValueError('Duplicate/invalid airport map variant')
  seen.add(identity)
  if meta.get('airport_icao')!=icao or meta.get('asset_version')!=maps['version'] or meta.get('projection')!='EPSG:3857' or meta.get('rotation_deg')!=0 or not overlay and meta.get('license')!='ODbL-1.0':raise ValueError('Airport map geographic identity differs')
  w,h=meta.get('width'),meta.get('height')
  low,high=(1,800) if overlay else (64,2048)
  if any(type(v)is not int or not low<=v<=high for v in (w,h)) or not overlay and variant=='mini' and (w,h)!=(800,800):raise ValueError('Invalid airport map dimensions')
  b=meta.get('rendering_bounds',{})
  extent=100000 if overlay else 240000
  if set(b)!={'min_x','max_x','min_y','max_y'} or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>20100000 for v in b.values()) or not 0<b['max_x']-b['min_x']<=extent or not 0<b['max_y']-b['min_y']<=extent:raise ValueError('Invalid projected airport map bounds')
  if stored:
   if variant!=maps['variant'] or type(meta.get('offset'))is not int or meta['offset']<16 or type(meta.get('bytes'))is not int or not 57<=meta['bytes']<=2*1024**2 or not re.fullmatch('[a-f0-9]{64}',meta.get('sha256','')) or 'url' in meta or 'key' in meta:raise ValueError('Invalid local airport map payload')
  elif not reference(asset,'.png') or meta.get('sha256')!=asset['sha256'] or meta.get('bytes')!=asset['bytes']:raise ValueError('Invalid captured airport map asset')
 if overlay:
  if seen!={(v,'diagram_overlay') for v in ((maps['variant'],) if stored else ('mini','plus'))}:raise ValueError('Missing airport overlay variant')
 elif stored:
  if seen!={(maps['variant'],'planeslate_dark'),(maps['variant'],'planeslate_light')}:raise ValueError('Both offline airport map themes are required')
 elif not {(v,'planeslate_'+s) for v in ('thumbnail','mini','plus') for s in ('dark','light')}<=seen:raise ValueError('Missing approved map variants')

def verify_png(raw,meta):
 from hashlib import sha256
 if len(raw)!=meta['bytes'] or sha256(raw).hexdigest()!=meta['sha256']:raise ValueError('Airport map checksum differs')
 with Image.open(io.BytesIO(raw)) as image:
  if image.format!='PNG' or image.size!=(meta['width'],meta['height']) or image.mode not in ('RGB','RGBA'):raise ValueError('Invalid airport map PNG')
  image.load()
  if meta.get('style')=='diagram_overlay':
   alpha=image.getchannel('A').getextrema() if image.mode=='RGBA' else (255,255)
   if alpha[0]==255 or alpha[1]==0:raise ValueError('Airport overlay must contain transparent and visible pixels')

def collect(rows,store,work):
 work.mkdir(exist_ok=True);images={};sources={}
 for airport in rows:
  maps=airport.get('maps');validate_maps(maps,airport['icao'])
  if not maps:continue
  for asset in maps['assets']:
   if asset['metadata']['variant'] in ('thumbnail','master'):continue
   digest=asset['sha256']
   if digest not in images:
    path=work/(digest+'.png');store.get(asset['key'],path,digest,asset['bytes']);raw=path.read_bytes();verify_png(raw,asset['metadata']);images[digest]=raw
  if maps.get('kind')=='overlay':continue
  geometry=maps.get('source',{}).get('geometry')
  if not reference(geometry,'.geojson'):raise ValueError('Published airport maps require licensed source geometry')
  path=work/(geometry['sha256']+'.geojson')
  if not path.exists():store.get(geometry['key'],path,geometry['sha256'],geometry['bytes'])
  raw=path.read_bytes()
  from hashlib import sha256
  if len(raw)!=geometry['bytes'] or sha256(raw).hexdigest()!=geometry['sha256']:raise ValueError('Airport map source checksum differs')
  source=json.loads(raw)
  if source.get('airport_icao')!=airport['icao'] or source.get('license')!='ODbL-1.0':raise ValueError('Airport map source license/identity differs')
  sources[airport['icao']+'.geojson']=raw
 return images,sources
