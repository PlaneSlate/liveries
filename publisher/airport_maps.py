"""OSM airport diagrams. Central generation only; devices consume PNG + EPSG:3857 metadata.

Source geometry is preserved. Only the drawing expands centerlines to a visible width.
OSM ways are joined only at identical node IDs in multipolygon relations.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw, ImageFont

ENDPOINTS=('https://overpass-api.de/api/interpreter','https://overpass.kumi.systems/api/interpreter')
MAX_SOURCE=24*1024*1024
ATTRIBUTION='© OpenStreetMap contributors'
LICENSE='https://www.openstreetmap.org/copyright'
VARIANTS={'thumbnail':(480,320),'mini':(800,800),'plus':(1280,720)}
PALETTES={
 'dark':dict(background='#061d25',boundary='#25464b',runway='#263338',edge='#d3e1e3',taxiway='#48858e',apron='#304d58',terminal='#9db8c5',building='#536e7b',gate='#72c6db',text='#c7dbe1'),
 'light':dict(background='#edf2f0',boundary='#c0cecd',runway='#495557',edge='#182c32',taxiway='#52808c',apron='#c4d2d5',terminal='#fafcfb',building='#a7b9c0',gate='#257889',text='#17333f')}

DIAGRAM_PALETTES={
 'light':dict(background='#F3F6F6',runway='#304C5A',taxiway='#C2D1D4',apron='#E0E6E7',terminal='#718994',building='#A7B4BB',text='#304C5A'),
 'dark':dict(background='#142831',runway='#C8DBE0',taxiway='#647F89',apron='#2C424D',terminal='#90A9B3',building='#526A76',text='#C8DBE0'),
}
DIAGRAM_ORDER=('apron','building','hangar','terminal','taxiway','runway')
# Estimated drawing widths in metres, used only when a centerline has no usable width.
DIAGRAM_WIDTHS={'taxiway':18,'runway':30}
DIAGRAM_MIN_PIXELS={'taxiway':2.0,'runway':3.0}

def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def now():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def coordinate(p):return len(p)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in p) and -180<=p[0]<=180 and -85.051128<=p[1]<=85.051128
def project(lon,lat):
 if not coordinate([lon,lat]):raise ValueError('Coordinate outside Web Mercator')
 return 6378137*math.radians(lon),6378137*math.log(math.tan(math.pi/4+math.radians(lat)/2))
def unproject(x,y):return [math.degrees(x/6378137),math.degrees(2*math.atan(math.exp(y/6378137))-math.pi/2)]
def points(g):
 if g['type']=='Point':return [g['coordinates']]
 if g['type']=='LineString':return g['coordinates']
 if g['type']=='Polygon':return [p for ring in g['coordinates'] for p in ring]
 if g['type']=='MultiPolygon':return [p for poly in g['coordinates'] for ring in poly for p in ring]
 raise ValueError('Unsupported geometry')
def bounds_of(features):
 p=[p for f in features for p in points(f['geometry'])]
 if not p:raise ValueError('No airport geometry')
 return dict(min_lon=min(v[0] for v in p),min_lat=min(v[1] for v in p),max_lon=max(v[0] for v in p),max_lat=max(v[1] for v in p))
def validate_bounds(b,center=None):
 if not isinstance(b,dict) or set(b)!={'min_lon','min_lat','max_lon','max_lat'}:raise ValueError('Invalid map bounds')
 if not coordinate([b['min_lon'],b['min_lat']]) or not coordinate([b['max_lon'],b['max_lat']]) or b['min_lon']>=b['max_lon'] or b['min_lat']>=b['max_lat']:raise ValueError('Invalid map extent')
 x,y=project(b['min_lon'],b['min_lat']);u,v=project(b['max_lon'],b['max_lat'])
 if max(u-x,v-y)>120000 or min(u-x,v-y)<10:raise ValueError('Unreasonable airport extent')
 if center and not(b['min_lon']<=center[0]<=b['max_lon'] and b['min_lat']<=center[1]<=b['max_lat']):raise ValueError('Airport coordinates outside extent')
 return b
def ring_ok(ring):
 if len(ring)<4 or ring[0]!=ring[-1] or len(ring)>12000 or len(set(map(tuple,ring[:-1])))<3:return False
 # A sweep on x avoids a quadratic scan for detailed airport perimeters.
 def orient(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
 active=[]
 segments=sorted((min(a[0],b[0]),max(a[0],b[0]),i,a,b) for i,(a,b) in enumerate(zip(ring,ring[1:])))
 for left,right,i,a,b in segments:
  active=[s for s in active if s[1]>=left]
  for _,_,j,c,d in active:
   if abs(i-j)==1 or {i,j}=={0,len(ring)-2}:continue
   if max(min(a[1],b[1]),min(c[1],d[1]))>min(max(a[1],b[1]),max(c[1],d[1])):continue
   if orient(a,b,c)*orient(a,b,d)<=0 and orient(c,d,a)*orient(c,d,b)<=0:return False
  active.append((left,right,i,a,b))
 return abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:])))>1e-12
def inside(p,ring):
 hit=False;x,y=p
 for a,b in zip(ring,ring[1:]):
  if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:hit=not hit
 return hit
def join_rings(parts):
 pending=[list(p) for p in parts];out=[]
 while pending:
  ring=pending.pop(0)
  while ring[0]!=ring[-1]:
   candidates=[(i,p) for i,p in enumerate(pending) if p[0]==ring[-1] or p[-1]==ring[-1]]
   if len(candidates)!=1:raise ValueError('Open or ambiguous relation ring')
   i,p=candidates[0];pending.pop(i);ring.extend((p if p[0]==ring[-1] else p[::-1])[1:])
  out.append(ring)
 return out
def kind(tags):
 a=tags.get('aeroway')
 if a in ('aerodrome','runway','taxiway','apron','terminal','gate','parking_position','helipad','hangar'):return a
 if tags.get('building'):return 'terminal' if tags['building']=='terminal' else 'building'
 if tags.get('highway')=='service':return 'service'
 return None
def normalize(raw,airport):
 if not isinstance(raw,dict) or not isinstance(raw.get('elements'),list) or len(raw['elements'])>120000:raise ValueError('Invalid/oversized Overpass response')
 if raw.get('remark'):raise ValueError('Incomplete Overpass response: '+str(raw['remark'])[:180])
 elements={};warnings=[]
 for e in raw['elements']:
  key=(e.get('type'),e.get('id'))
  if key in elements:
   if elements[key]!=e:raise ValueError('Conflicting duplicate OSM element')
   warnings.append('Duplicate %s/%s omitted'%key);continue
  elements[key]=e
 def way_coords(e):
  if e.get('geometry'):
   p=[[n.get('lon'),n.get('lat')] for n in e['geometry']]
  else:
   nodes=[elements.get(('node',n)) for n in e.get('nodes',[])]
   if not nodes or any(n is None for n in nodes):raise ValueError('Missing referenced OSM nodes')
   p=[[n.get('lon'),n.get('lat')] for n in nodes]
  if len(p)<2 or not all(coordinate(v) for v in p):raise ValueError('Invalid line coordinates')
  return p
 features=[];consumed=set()
 for e in raw['elements']:
  if e.get('type')=='relation' and kind(e.get('tags',{})) and e.get('tags',{}).get('type')=='multipolygon':
   try:
    groups={'outer':[],'inner':[]};coords={}
    for m in e.get('members',[]):
     if m.get('type')!='way':continue
     role=m.get('role') or 'outer'
     if role not in groups:continue
     w=elements.get(('way',m['ref']))
     if not w:raise ValueError('Missing relation way')
     ids=w.get('nodes');p=way_coords(w)
     if not ids or len(ids)!=len(p):raise ValueError('Missing relation node identities')
     groups[role].append(ids);coords.update(zip(ids,p))
    outer=[[coords[n] for n in r] for r in join_rings(groups['outer'])];inner=[[coords[n] for n in r] for r in join_rings(groups['inner'])]
    if not outer or not all(ring_ok(r) for r in outer+inner):raise ValueError('Invalid multipolygon rings')
    polys=[[r] for r in outer]
    for hole in inner:
     candidates=[p for p in polys if inside(hole[0],p[0])]
     if len(candidates)!=1:raise ValueError('Unassigned multipolygon hole')
     candidates[0].append(hole)
    features.append(dict(type='Feature',id='relation/'+str(e['id']),properties={**e.get('tags',{}),'feature':kind(e['tags'])},geometry=dict(type='MultiPolygon',coordinates=polys)))
    consumed.update(m['ref'] for m in e['members'] if m.get('type')=='way')
   except ValueError as ex:warnings.append('relation/%s: %s'%(e['id'],ex))
 seen=set()
 for e in raw['elements']:
  identity='%s/%s'%(e.get('type'),e.get('id'));tags=e.get('tags',{});k=kind(tags)
  if not k or e.get('type')=='relation' or identity in seen:continue
  seen.add(identity)
  if e['type']=='way' and e['id'] in consumed and not tags.get('aeroway'):continue
  try:
   if e['type']=='node':
    p=[e.get('lon'),e.get('lat')]
    if not coordinate(p):raise ValueError('Invalid point')
    g=dict(type='Point',coordinates=p)
   elif e['type']=='way':
    p=way_coords(e);closed=e.get('nodes',[0,1])[0]==e.get('nodes',[0,1])[-1] and p[0]==p[-1]
    area=tags.get('area')=='yes' or k in ('aerodrome','apron','terminal','building','hangar')
    if area:
     if not closed or not ring_ok(p):raise ValueError('Invalid/unclosed polygon')
     g=dict(type='Polygon',coordinates=[p])
    else:g=dict(type='LineString',coordinates=p)
   else:continue
   features.append(dict(type='Feature',id=identity,properties={**{key:tags[key] for key in ('name','ref','width','icao','iata','surface') if key in tags},'feature':k},geometry=g))
  except ValueError as ex:warnings.append(identity+': '+str(ex))
 if not features:raise ValueError('No usable airport geometry')
 center=[airport['longitude'],airport['latitude']]
 accepted=[]
 for f in features:
  if any(math.hypot((p[0]-center[0])*111320*math.cos(math.radians(center[1])),(p[1]-center[1])*111320)>40000 for p in points(f['geometry'])):
   warnings.append(f['id']+': outside expected airport area');continue
  accepted.append(f)
 perimeters=[f['geometry'] for f in accepted if f['properties']['feature']=='aerodrome' and f['geometry']['type'] in ('Polygon','MultiPolygon')]
 def in_airport(p):
  return any(inside(p,poly[0]) and not any(inside(p,h) for h in poly[1:]) for g in perimeters for poly in ([g['coordinates']] if g['type']=='Polygon' else g['coordinates']))
 if perimeters:
  accepted=[f for f in accepted if f['properties']['feature'] not in ('building','service') or all(in_airport(p) for p in points(f['geometry']))]
 else:warnings.append('No polygon perimeter: optional buildings/roads use the explicitly bounded query area')
 bounds=validate_bounds(bounds_of(accepted),center)
 counts=dict(Counter(f['properties']['feature'] for f in accepted))
 for k in ('runway','taxiway','apron','terminal','aerodrome'):
  if not counts.get(k):warnings.append('Missing mapped '+k)
 return dict(type='FeatureCollection',airport_icao=airport['icao'],features=accepted,bounds=bounds,counts=counts,warnings=sorted(set(warnings)),source='OpenStreetMap',source_timestamp=raw.get('osm3s',{}).get('timestamp_osm_base'),retrieved_at=now(),license='ODbL-1.0',license_url=LICENSE,attribution=ATTRIBUTION)

def overpass(query,endpoint=ENDPOINTS[0]):
 if endpoint not in ENDPOINTS:raise ValueError('Unapproved Overpass endpoint')
 request=urllib.request.Request(endpoint,data=urllib.parse.urlencode({'data':query}).encode(),headers={'User-Agent':'PlaneSlate-AirportMaps/1.0 (+https://planeslate.com)','Accept':'application/json'})
 for attempt in range(3):
  try:
   with urllib.request.urlopen(request,timeout=80) as response:raw=response.read(MAX_SOURCE+1)
   if len(raw)>MAX_SOURCE:raise ValueError('Overpass response exceeds limit')
   return json.loads(raw)
  except (OSError,TimeoutError) as ex:
   if attempt==2:raise ValueError('Overpass unavailable; previous approved maps retained') from ex
   time.sleep(3*2**attempt)

def fetch_geometry(airport,*,bbox=None,osm_id=None,endpoint=ENDPOINTS[0]):
 if not re.fullmatch('[A-Z0-9]{4}',airport.get('icao','')) or not coordinate([airport.get('longitude'),airport.get('latitude')]):raise ValueError('Invalid airport identity')
 lat,lon=airport['latitude'],airport['longitude']
 if bbox:validate_bounds(bbox,[lon,lat])
 if osm_id:
  if not re.fullmatch('(node|way|relation)/[1-9][0-9]{0,11}',osm_id):raise ValueError('Invalid OSM identity')
  typ,id=osm_id.split('/');lookup=f'{typ}({id});'
 else:
  lookup=f'nwr["aeroway"="aerodrome"]["icao"="{airport["icao"]}"];'
  if re.fullmatch('[A-Z]{3}',airport.get('iata','')):lookup+=f'nwr(around:35000,{lat},{lon})["aeroway"="aerodrome"]["iata"="{airport["iata"]}"];'
  lookup+=f'nwr(around:5000,{lat},{lon})["aeroway"="aerodrome"];'
 source=overpass('[out:json][timeout:60];('+lookup+');out body geom;',endpoint)
 candidates=[]
 for e in source.get('elements',[]):
  tags=e.get('tags',{});p=e.get('geometry',[])+[p for m in e.get('members',[]) for p in m.get('geometry',[])]
  if p:
   b=dict(min_lon=min(n['lon'] for n in p),max_lon=max(n['lon'] for n in p),min_lat=min(n['lat'] for n in p),max_lat=max(n['lat'] for n in p))
   if b['min_lon']<=lon<=b['max_lon'] and b['min_lat']<=lat<=b['max_lat']:candidates.append((0 if tags.get('icao')==airport['icao'] else 1,e,b))
 if bbox:b=bbox
 elif candidates:
  candidates.sort(key=lambda item:item[0]);_,selected,b=candidates[0]
  # Adaptive extent from the actual perimeter; include adjacent facilities.
  dx=(b['max_lon']-b['min_lon'])*.08;dy=(b['max_lat']-b['min_lat'])*.08
  b={**b,'min_lon':b['min_lon']-dx,'max_lon':b['max_lon']+dx,'min_lat':b['min_lat']-dy,'max_lat':b['max_lat']+dy}
 else:raise ValueError('No matching airport perimeter. Supply an explicit bounded extent or OSM perimeter ID.')
 validate_bounds(b,[lon,lat]);extent=','.join(str(b[k]) for k in ('min_lat','min_lon','max_lat','max_lon'))
 query=f'[out:json][timeout:60];(nwr["aeroway"]({extent});way["building"]({extent});way["highway"="service"]({extent}););(._;>;);out body geom;'
 raw=overpass(query,endpoint);geo=normalize(raw,airport);geo['query_bounds']=b;geo['endpoint']=endpoint
 return raw,geo

def transform(bounds,width,height,padding=0.08,circular=False):
 validate_bounds(bounds)
 if not(64<=width<=2048 and 64<=height<=2048 and 0.02<=padding<=.3):raise ValueError('Invalid viewport/padding')
 x,y=project(bounds['min_lon'],bounds['min_lat']);u,v=project(bounds['max_lon'],bounds['max_lat'])
 scale=min(width*(1-2*padding)/(u-x),height*(1-2*padding)/(v-y))
 if circular:scale=min(scale,min(width,height)*(1-2*padding)/math.hypot(u-x,v-y))
 cx,cy=(x+u)/2,(y+v)/2
 return dict(min_x=cx-width/2/scale,min_y=cy-height/2/scale,max_x=cx+width/2/scale,max_y=cy+height/2/scale)
def pixel(lon,lat,meta):
 x,y=project(lon,lat);b=meta['rendering_bounds']
 return (x-b['min_x'])/(b['max_x']-b['min_x'])*meta['width'],(b['max_y']-y)/(b['max_y']-b['min_y'])*meta['height']
def physical_width(feature):
 value=str(feature['properties'].get('width','')).strip()
 match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(m|ft|\x27)?',value)
 if not match:return None
 width=float(match[1])*(.3048 if match[2] in ('ft',"'") else 1)
 return width if .5<=width<=200 else None

def diagram_viewport(features,bounds,width,height,padding,circular,manual):
 extents=[]
 for feature in features:
  kind=feature['properties']['feature'];geometry=feature['geometry']
  width_m=(physical_width(feature) or DIAGRAM_WIDTHS[kind]) if geometry['type']=='LineString' and kind in DIAGRAM_WIDTHS else 0
  for lon,lat in points(geometry):
   x,y=project(lon,lat)
   extents.append((x,y,width_m/(2*math.cos(math.radians(lat)))))
 if manual or not extents:
  left,bottom=project(bounds['min_lon'],bounds['min_lat']);right,top=project(bounds['max_lon'],bounds['max_lat'])
  extents.extend((x,y,0) for x in (left,right) for y in (bottom,top))
 left=min(x-r for x,y,r in extents);right=max(x+r for x,y,r in extents)
 bottom=min(y-r for x,y,r in extents);top=max(y+r for x,y,r in extents)
 cx,cy=(left+right)/2,(bottom+top)/2
 available_x=width*(.5-padding)-DIAGRAM_MIN_PIXELS['runway']
 available_y=height*(.5-padding)-DIAGRAM_MIN_PIXELS['runway']
 scale=min(available_x/max(5,right-cx),available_y/max(5,top-cy))
 if circular:
  radius=max(math.hypot(x-cx,y-cy)+r for x,y,r in extents)
  scale=min(scale,min(available_x,available_y)/max(5,radius))
 return dict(min_x=cx-width/(2*scale),min_y=cy-height/(2*scale),max_x=cx+width/(2*scale),max_y=cy+height/(2*scale))

def draw_diagram_polygon(image,color,exterior,holes):
 # A hole affects only its own feature. Overlapping pavement is drawn as a union.
 left=max(0,math.floor(min(x for x,y in exterior)));top=max(0,math.floor(min(y for x,y in exterior)))
 right=min(image.width,math.ceil(max(x for x,y in exterior))+1);bottom=min(image.height,math.ceil(max(y for x,y in exterior))+1)
 if left>=right or top>=bottom:return
 mask=Image.new('L',(right-left,bottom-top));pen=ImageDraw.Draw(mask)
 local=lambda ring:[(x-left,y-top) for x,y in ring]
 pen.polygon(local(exterior),fill=255)
 for hole in holes:pen.polygon(local(hole),fill=0)
 image.paste(color,(left,top,right,bottom),mask)

def render(geo,variant='plus',style='dark',*,size=None,padding=.08,density='none',optional=True,bbox=None,version=1,design='diagram',layers=None,runway_labels=False):
 if style not in PALETTES or variant not in (*VARIANTS,'master'):raise ValueError('Invalid map style/variant')
 if density not in ('none','essential','normal','detailed'):raise ValueError('Invalid label density')
 width,height=size or (VARIANTS.get(variant) or (2048,2048));bounds=validate_bounds(bbox or geo['bounds'])
 # Manual bounds may enlarge the diagram, but may not silently clip source infrastructure.
 b=geo['bounds']
 if bounds['min_lon']>b['min_lon'] or bounds['min_lat']>b['min_lat'] or bounds['max_lon']<b['max_lon'] or bounds['max_lat']<b['max_lat']:raise ValueError('Extent clips source geometry; fetch the adjusted extent first')
 meta=dict(schema=1,airport_icao=geo['airport_icao'],style='planeslate_'+style,variant=variant,width=width,height=height,projection='EPSG:3857',rotation_deg=0,bounds=bounds,rendering_bounds=transform(bounds,width,height,padding,variant=='mini'),asset_version=version,generated_at=now(),source=geo['source'],source_timestamp=geo['source_timestamp'],retrieved_at=geo['retrieved_at'],attribution=ATTRIBUTION,license='ODbL-1.0',license_url=LICENSE,counts=geo['counts'],warnings=geo['warnings'])
 if design not in ('diagram','legacy'):raise ValueError('Invalid map design')
 diagram=design=='diagram';features=geo['features'];layer_options=layers or {}
 if diagram:
  layer_key={'building':'buildings','hangar':'buildings','terminal':'buildings','apron':'aprons','taxiway':'taxiways'}
  features=[f for f in features if f['properties']['feature'] in DIAGRAM_ORDER and f['geometry']['type']!='Point'
            and layer_options.get(layer_key.get(f['properties']['feature']),True)]
  meta['rendering_bounds']=diagram_viewport(features,bounds,width,height,padding,variant=='mini',bbox is not None)
 pal=DIAGRAM_PALETTES[style] if diagram else PALETTES[style]
 aa=2 if diagram else 1
 image=Image.new('RGB',(width*aa,height*aa),pal['background']);draw=ImageDraw.Draw(image);labels=[];label_ends={}
 def screen(p):
  x,y=pixel(*p,meta)
  return x*aa,y*aa
 font_path=Path(__file__).resolve().parents[1]/'assets/fonts/Jost-Variable.ttf'
 fonts=[str(Path(__file__).with_name('Jost-Variable.ttf')),str(font_path),'C:/Windows/Fonts/segoeui.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
 def font(size):
  for path in fonts:
   try:return ImageFont.truetype(path,size)
   except OSError:pass
  return ImageFont.load_default(size=size)
 fnt=font(max(10,round(width/90)));big=font(max(12,round(width/65)))
 order=DIAGRAM_ORDER if diagram else ['aerodrome','service','apron','building','hangar','terminal','taxiway','runway','parking_position','gate','helipad']
 estimated=Counter()
 for f in sorted(features,key=lambda f:order.index(f['properties']['feature'])):
  p=f['properties'];k=p['feature'];g=f['geometry'];c=g['coordinates']
  if not diagram and not optional and k in ('service','building','hangar','helipad','gate','parking_position'):continue
  color=pal.get(k,pal['building']);geo_points=points(g)
  pts=[screen(p) for p in geo_points]
  if g['type'] in ('Polygon','MultiPolygon'):
   polygons=[c] if g['type']=='Polygon' else c
   for poly in polygons:
    exterior=[screen(q) for q in poly[0]]
    if diagram:
     draw_diagram_polygon(image,color,exterior,[[screen(q) for q in hole] for hole in poly[1:]])
     continue
    if k=='aerodrome':draw.line(exterior,fill=color,width=1,joint='curve');continue
    mask=Image.new('L',(width,height));pen=ImageDraw.Draw(mask);pen.polygon(exterior,fill=255)
    for hole in poly[1:]:pen.polygon([pixel(*q,meta) for q in hole],fill=0)
    image.paste(color,(0,0),mask);draw.line(exterior,fill=pal['edge'] if k=='runway' else pal['boundary'],width=1,joint='curve')
  elif g['type']=='LineString':
   line_width=2 if k=='taxiway' else 1
   if diagram and k in DIAGRAM_WIDTHS:
    w=physical_width(f)
    if w is None:estimated[k]+=1;w=DIAGRAM_WIDTHS[k]
    scale=width/(meta['rendering_bounds']['max_x']-meta['rendering_bounds']['min_x'])
    line_width=round(aa*max(DIAGRAM_MIN_PIXELS[k],w*scale/math.cos(math.radians(geo_points[0][1]))))
   elif not diagram and k=='runway':
    # Use only recorded physical width; a missing width stays a diagram line.
    w=str(p.get('width','')).removesuffix(' m')
    if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',w) and 0<float(w)<200:
     scale=width/(meta['rendering_bounds']['max_x']-meta['rendering_bounds']['min_x']);line_width=max(2,round(float(w)/math.cos(math.radians(geo_points[0][1]))*scale))
    draw.line(pts,fill=pal['edge'],width=line_width+2,joint='curve')
   draw.line(pts,fill=color,width=line_width,joint='curve')
   if diagram and k=='taxiway':
    radius=line_width/2
    for x,y in pts:draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=color)
  elif k in ('gate','parking_position','helipad'):
   x,y=pts[0];draw.ellipse((x-2,y-2,x+2,y+2),fill=pal['gate'])
  label=(p.get('name') or p.get('ref')) if k=='terminal' else p.get('ref') or p.get('name')
  priority={'runway':0,'terminal':1,'taxiway':2,'gate':3,'parking_position':4}.get(k,9)
  show_label=(runway_labels and k=='runway') if diagram else (density!='none' and priority<5 and (priority<2 or variant!='thumbnail' and density!='essential' and (priority<3 or density=='detailed')))
  if show_label and label:
   pts=[(x/aa,y/aa) for x,y in pts]
   x,y=pts[len(pts)//2] if g['type']=='LineString' else (sum(v[0] for v in pts)/len(pts),sum(v[1] for v in pts)/len(pts))
   # Runway references are sourced, never inferred from an angle. A two-end ref
   # is split only when its supplied directions agree with the measured axis.
   ends=re.fullmatch(r'(\d{2}[LCR]?)/(\d{2}[LCR]?)',str(label)) if k=='runway' and g['type']=='LineString' else None
   if ends:label_ends.setdefault(str(label),[]).extend((pts[0],pts[-1]))
   else:labels.append((priority,str(label)[:80],x,y))
 # Multiple OSM ways can describe one physical runway. Place labels at its
 # extreme sourced endpoints, without drawing a connection between the ways.
 if aa>1:image=image.resize((width,height),Image.Resampling.LANCZOS);draw=ImageDraw.Draw(image)
 for reference,endpoints in label_ends.items():
  a,b=max(((a,b) for a in endpoints for b in endpoints),key=lambda pair:math.dist(*pair));heading=math.degrees(math.atan2(b[0]-a[0],a[1]-b[1]))%360
  pair=reference.split('/');delta=abs((heading-int(pair[0][:2])*10+180)%360-180)
  if delta<30 or delta>150:
   if delta>150:pair.reverse()
   labels.extend((0,text,*p) for text,p in zip(pair,(a,b)))
  else:labels.append((0,reference,(a[0]+b[0])/2,(a[1]+b[1])/2))
 placed=[];placed_names=set();limit=12 if variant=='thumbnail' else 55 if variant=='mini' else 100
 for priority,text,x,y in sorted(labels):
  identity=(priority,text)
  if identity in placed_names:continue
  ft=big if priority==0 else fnt;box=draw.textbbox((0,0),text,font=ft);w,h=box[2]-box[0]+8,box[3]-box[1]+6
  for dx,dy in ((0,-h),(0,0),(0,h),(w/2+8,0),(-w/2-8,0)):
   left,top=x+dx-w/2,y+dy-h/2;right,bottom=left+w,top+h
   if left<8 or right>width-8 or top<8 or bottom>height-24:continue
   if variant=='mini' and any(math.hypot(a-width/2,b-height/2)>width/2-22 for a,b in ((left,top),(right,top),(left,bottom),(right,bottom))):continue
   if any(left<b and right>a and top<d and bottom>c for a,c,b,d in placed):continue
   draw.rounded_rectangle((left,top,right,bottom),radius=2,fill=pal['background']);draw.text((left+4,top+3-box[1]),text,font=ft,fill=pal['text']);placed.append((left-3,top-3,right+3,bottom+3));placed_names.add(identity);break
  if len(placed)>=limit:break
 caption=ATTRIBUTION+' · ODbL'
 draw.text((width/2,height-(42 if variant=='mini' else 17)),caption,font=font(10 if width<800 else 12),fill=pal['text'],anchor='mt')
 meta['runways']=[dict(ref=f['properties'].get('ref',''),points=[list(pixel(*p,meta)) for p in f['geometry']['coordinates']]) for f in geo['features'] if f['properties']['feature']=='runway' and f['geometry']['type']=='LineString' and len(f['geometry']['coordinates'])<=512]
 raw=io.BytesIO();image.save(raw,format='PNG',optimize=True);data=raw.getvalue();meta.update(bytes=len(data),sha256=sha(data),labels=len(placed))
 if diagram:
  meta['estimated_widths']=dict(estimated)
  meta['warnings']=[*meta['warnings'],*(f'{count} {kind} centerline(s): estimated {DIAGRAM_WIDTHS[kind]} m drawing width' for kind,count in sorted(estimated.items()))]
 return data,meta

def main():
 p=argparse.ArgumentParser();p.add_argument('--icao',default='LSZH');p.add_argument('--iata',default='ZRH');p.add_argument('--latitude',type=float,default=47.4647);p.add_argument('--longitude',type=float,default=8.54917);p.add_argument('--output',type=Path,required=True);p.add_argument('--source',type=Path);p.add_argument('--endpoint',choices=ENDPOINTS,default=ENDPOINTS[0]);args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
 airport=dict(icao=args.icao,iata=args.iata,latitude=args.latitude,longitude=args.longitude)
 if args.source:raw=json.loads(args.source.read_bytes());geo=normalize(raw,airport)
 else:raw,geo=fetch_geometry(airport,endpoint=args.endpoint)
 (args.output/'source.json').write_bytes(encoded(raw));(args.output/'geometry.geojson').write_bytes(encoded(geo))
 for variant in VARIANTS:
  for style in PALETTES:
   png,meta=render(geo,variant,style);(args.output/f'{variant}-{style}.png').write_bytes(png);(args.output/f'{variant}-{style}.json').write_bytes(encoded(meta))
 print(json.dumps(dict(airport=args.icao,counts=geo['counts'],warnings=geo['warnings'],bounds=geo['bounds'])))
if __name__=='__main__':main()
