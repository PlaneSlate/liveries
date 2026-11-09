"""Bounded, advisory geometry extraction for the existing Airport Maps job."""
from collections import Counter
from ctypes import byref, c_float, c_int, c_uint
from hashlib import sha256
import io
import math

from PIL import Image, ImageOps

from airport_maps import bounds_of, coordinate, encoded, physical_width, points, project, ring_ok, unproject


MAX_DOCUMENT = 10 * 1024**2
MAX_IMAGE_PIXELS = 36_000_000
MAX_PAGES = 50
MAX_CANDIDATES = 180
KINDS = ('runway', 'taxiway', 'apron', 'terminal', 'building', 'hangar')


def read_diagram(raw, format, page):
    """Return a sanitized preview and any safely separable vector outlines."""
    if len(raw) > MAX_DOCUMENT or type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError('Airport diagram or page exceeds the analysis limit')
    if format == 'pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(raw) as document:
            page_count = len(document)
            if page_count > MAX_PAGES or page > page_count:
                raise ValueError(f'Select a page between 1 and {min(page_count, MAX_PAGES)}')
            sheet = document[page - 1]
            try:
                width, height = sheet.get_size()
                if not 1 <= min(width, height) <= max(width, height) <= 10000:
                    raise ValueError('Unsupported PDF page size')
                bitmap = sheet.render(scale=1800 / max(width, height), draw_annots=False)
                try:
                    image = bitmap.to_pil().convert('RGB')
                finally:
                    bitmap.close()
                vectors = vector_outlines(sheet, image.size, pdfium.raw)
            finally:
                sheet.close()
    else:
        with Image.open(io.BytesIO(raw)) as source:
            if (format not in ('png', 'jpeg') or source.format != format.upper()
                    or source.width * source.height > MAX_IMAGE_PIXELS or page != 1
                    or getattr(source, 'n_frames', 1) != 1):
                raise ValueError('Select a single PNG or JPEG of at most 36 megapixels')
            source.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            rgba = ImageOps.exif_transpose(source).convert('RGBA')
            image = Image.new('RGB', rgba.size, 'white')
            image.paste(rgba, mask=rgba.getchannel('A'))
        page_count, vectors = 1, []
    if min(image.size) < 160:
        raise ValueError('Diagram is too small for geometry comparison')
    return image, page_count, vectors


def vector_outlines(sheet, size, pdf):
    # Mixed paths, curved subpaths and form objects are handled by raster analysis.
    # Text objects, unfilled strokes, frames and tiny symbols are excluded here.
    if sheet.get_rotation():
        return []
    left, bottom, right, top = sheet.get_bbox()
    vectors = []
    for index, obj in enumerate(sheet.get_objects(filter=[pdf.FPDF_PAGEOBJ_PATH], max_depth=1)):
        if index >= 6000:
            break
        count = pdf.FPDFPath_CountSegments(obj.raw)
        mode, stroke = c_int(), c_int()
        if not 4 <= count <= 200 or not pdf.FPDFPath_GetDrawMode(obj.raw, byref(mode), byref(stroke)) or not mode.value:
            continue
        red, green, blue, alpha = (c_uint() for _ in range(4))
        if not pdf.FPDFPageObj_GetFillColor(obj.raw, byref(red), byref(green), byref(blue), byref(alpha)) or alpha.value < 220:
            continue
        color = [red.value, green.value, blue.value]
        if max(color)-min(color) > 20 or max(color) > 240:
            continue
        matrix = obj.get_matrix()
        outline = []
        for i in range(count):
            segment = pdf.FPDFPath_GetPathSegment(obj.raw, i)
            x, y = c_float(), c_float()
            if (pdf.FPDFPathSegment_GetType(segment) != (pdf.FPDF_SEGMENT_MOVETO if i == 0 else pdf.FPDF_SEGMENT_LINETO)
                    or not pdf.FPDFPathSegment_GetPoint(segment, byref(x), byref(y))):
                break
            a, b = matrix.on_point(x.value, y.value)
            outline.append(((a-left)*size[0]/(right-left), (top-b)*size[1]/(top-bottom)))
        if len(outline) == count and (pdf.FPDFPathSegment_GetClose(segment) or math.dist(outline[0], outline[-1]) < 1):
            vectors.append((round(sum(color)/3), outline))
    return vectors


def runway_lines(dark, width):
    import cv2
    import numpy as np
    # Eliminate fine chart grids and text strokes before finding long pavement axes.
    thickness = max(3, round(width*.003) | 1)
    core = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((thickness, thickness), np.uint8))
    minimum = max(35, round(width*.12))
    lines = cv2.HoughLinesP(core, 1, np.pi/720, max(24, minimum//2), minLineLength=minimum, maxLineGap=max(4, round(width*.012)))
    merged = []
    for a, b, c, d in sorted((row[0].tolist() for row in lines) if lines is not None else [],
                              key=lambda line: (-math.dist(line[:2], line[2:]), line))[:800]:
        start, end = np.array((a,b), float), np.array((c,d), float)
        length = np.linalg.norm(end-start)
        axis = (end-start)/length
        for group in merged:
            origin, direction, low, high = group
            offset = np.array([-direction[1], direction[0]])
            bounds = sorted([np.dot(start-origin,direction), np.dot(end-origin,direction)])
            if (abs(np.dot(axis,direction)) > .998 and max(abs(np.dot(start-origin,offset)),abs(np.dot(end-origin,offset))) < width*.009
                    and bounds[0] <= high+width*.008 and bounds[1] >= low-width*.008):
                group[2:] = [min(low,bounds[0]),max(high,bounds[1])]
                break
        else:
            if len(merged)<40:
                merged.append([start,axis,0,length])
    return [(list(origin+axis*low), list(origin+axis*high)) for origin,axis,low,high in merged]


def structural_shapes(image, vectors):
    import cv2
    import numpy as np
    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    # Grayscale airport diagrams commonly use solid dark buildings and gray pavement.
    dark = cv2.inRange(gray, 0, 82)
    pavement = cv2.inRange(gray, 145, 228)
    if vectors:
        for tone, outline in vectors:
            contour=np.round(outline).astype(np.int32)
            if cv2.contourArea(contour) > width*height*.000035:
                if tone <= 82:cv2.fillPoly(dark,[contour],255)
                elif 145 <= tone <= 228:cv2.fillPoly(pavement,[contour],255)
    axes = runway_lines(dark, width)
    for start,end in axes:
        cv2.line(dark,tuple(round(x) for x in start),tuple(round(x) for x in end),0,max(5,round(width*.018)))
    kernel=lambda fraction:cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(max(3,round(width*fraction)|1),)*2)
    buildings=cv2.morphologyEx(dark,cv2.MORPH_OPEN,kernel(.0035))
    aprons=cv2.morphologyEx(pavement,cv2.MORPH_OPEN,kernel(.015))
    taxiways=cv2.morphologyEx(cv2.subtract(pavement,aprons),cv2.MORPH_OPEN,kernel(.002))
    result=[('runway','LineString',[a,b]) for a,b in axes]
    for kind,mask,minimum in [('building',buildings,.000045),('apron',aprons,.00018),('taxiway',taxiways,.000048)]:
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours,key=cv2.contourArea,reverse=True)[:70]:
            area=cv2.contourArea(contour)
            x,y,w,h=cv2.boundingRect(contour)
            if not width*height*minimum<=area<=width*height*.11 or x<=1 or y<=1 or x+w>=width-1 or y+h>=height-1:
                continue
            if kind=='building' and (max(w,h)>7*min(w,h) or area<.33*w*h):
                continue
            outline=cv2.approxPolyDP(contour, max(.9,width*.0008), True)[:,0,:].tolist()
            if 3<=len(outline)<=220:
                result.append((kind,'Polygon',outline+[outline[0]]))
    return result[:MAX_CANDIDATES]


def similarity(source,target):
    # Uniform scale, rotation and translation only. The page Y axis is flipped.
    src=[complex(x,-y) for x,y in source];dst=[complex(x,y) for x,y in target]
    center=sum(src)/len(src);origin=sum(dst)/len(dst)
    denominator=sum(abs(p-center)**2 for p in src)
    if denominator<16:
        raise ValueError('Control points are too close together')
    scale=sum((q-origin)*(p-center).conjugate() for p,q in zip(src,dst))/denominator
    return scale,origin-scale*center


def mapped(transform, point):
    value=transform[0]*complex(point[0],-point[1])+transform[1]
    return value.real,value.imag


def runway_controls(geo):
    groups={}
    for feature in geo['features']:
        if feature['properties']['feature']=='runway' and feature['geometry']['type']=='LineString':
            key=feature['properties'].get('ref') or feature['id']
            coords=feature['geometry']['coordinates']
            groups.setdefault(key,[]).extend([project(*coords[0]),project(*coords[-1])])
    return [(key,max(((a,b) for a in nodes for b in nodes),key=lambda ends:math.dist(*ends))) for key,nodes in sorted(groups.items())[:24]]


def align(shapes, geo, size, controls):
    import numpy as np
    width,height=size
    ground_factor=math.cos(math.radians((geo['bounds']['min_lat']+geo['bounds']['max_lat'])/2))
    axes=[coords for kind,typ,coords in shapes if kind=='runway'][:24]
    known=runway_controls(geo)
    if controls:
        source=[(p['image'][0]*width,p['image'][1]*height) for p in controls]
        target=[project(*p['geo']) for p in controls]
        b=geo['bounds']
        if any(not b['min_lon']<=p['geo'][0]<=b['max_lon'] or not b['min_lat']<=p['geo'][1]<=b['max_lat'] for p in controls):
            raise ValueError('Select control points within the OSM airport extent')
        if len(source)<3 or np.linalg.svd(np.array(source)-np.mean(source,axis=0),compute_uv=False)[1]<max(size)*.012:
            raise ValueError('Use at least three well separated, non-collinear control points')
        transform=similarity(source,target)
        errors=[math.dist(mapped(transform,p),q)*ground_factor for p,q in zip(source,target)]
        method='manual';matches=[]
    elif len(axes)>=3 and len(known)>=3:
        a=np.array(axes);g=np.array([ends for _,ends in known]);candidates={}
        for axis in axes:
            for _,ends in known:
                for target_ends in (ends,ends[::-1]):
                    guess=similarity(axis,target_ends)
                    scale,shift=guess
                    if not .1<=abs(scale)*ground_factor<=80:
                        continue
                    z=scale*(a[:,:,0]-1j*a[:,:,1])+shift
                    predicted=np.stack([z.real,z.imag],axis=2)
                    direct=np.linalg.norm(predicted[:,None]-g[None],axis=-1).max(axis=-1)
                    reverse=np.linalg.norm(predicted[:,None]-g[None,:,::-1],axis=-1).max(axis=-1)
                    costs=np.minimum(direct,reverse)*ground_factor
                    used_a=set();used_g=set();pairs=[]
                    for i,j in sorted(zip(*np.where(costs<165)),key=lambda ij:costs[ij]):
                        if i not in used_a and j not in used_g:
                            used_a.add(i);used_g.add(j);pairs.append((int(i),int(j),bool(reverse[i,j]<direct[i,j])))
                    if len(pairs)>=3:
                        candidates[tuple(sorted(pairs))]=True
        solutions=[]
        for pairs in candidates:
            source=[point for i,j,reverse in pairs for point in axes[i]]
            target=[point for i,j,reverse in pairs for point in (known[j][1][::-1] if reverse else known[j][1])]
            guess=similarity(source,target)
            errors=[math.dist(mapped(guess,p),q)*ground_factor for p,q in zip(source,target)]
            if max(errors)<=165:
                solutions.append((len(pairs),sum(e*e for e in errors)/len(errors),guess,pairs,errors))
        if not solutions:
            return None, {'method':'none','usable':False,'message':'No consistent runway match. Set manual control points.'}
        solutions.sort(key=lambda s:(-s[0],s[1]));count,loss,transform,pairs,errors=solutions[0]
        if len(solutions)>1 and solutions[1][0]==count and solutions[1][1]<loss+400:
            if math.dist(mapped(transform,(width/2,height/2)),mapped(solutions[1][2],(width/2,height/2)))*ground_factor>50:
                return None, {'method':'none','usable':False,'message':'Several runway matches fit. Set manual control points.'}
        method='runway-match';matches=[dict(candidate=i+1,ref=known[j][0]) for i,j,_ in pairs]
    else:
        return None, {'method':'none','usable':False,'message':'Three matched runway axes are needed for automatic alignment. Set manual control points.'}
    rms=math.sqrt(sum(e*e for e in errors)/len(errors))
    usable=.1<=abs(transform[0])*ground_factor<=80 and rms<=(45 if controls else 80) and max(errors)<=(80 if controls else 165)
    return transform, dict(method=method,usable=usable,rms_m=round(rms,1),max_m=round(max(errors),1),
                           matches=matches,matrix=[[z.real,z.imag] for z in transform],controls=controls,
                           message='Review the alignment before accepting geometry.' if usable else 'Alignment error exceeds the review limit.')


def osm_masks(geo, transform, size):
    import cv2
    import numpy as np
    masks={key:np.zeros((size[1],size[0]),dtype=np.uint8) for key in (*KINDS,'aerodrome')}
    def pixels(coords):
        page=[(complex(*project(*p))-transform[1])/transform[0] for p in coords]
        return np.array([(round(p.real),round(-p.imag)) for p in page],dtype=np.int32)
    for feature in geo['features']:
        kind=feature['properties']['feature'];g=feature['geometry']
        if kind not in masks or g['type']=='Point':continue
        mask=masks[kind]
        if g['type']=='LineString':
            scale=abs(transform[0])*math.cos(math.radians(g['coordinates'][0][1]))
            width=physical_width(feature) or (40 if kind=='runway' else 20)
            cv2.polylines(mask,[pixels(g['coordinates'])],False,255,max(3,round(width/scale)))
        else:
            for polygon in [g['coordinates']] if g['type']=='Polygon' else g['coordinates']:
                cv2.fillPoly(mask,[pixels(ring) for ring in polygon],255)
    return masks


def classify(shapes, transform, alignment, geo, size):
    import cv2
    import numpy as np
    masks=osm_masks(geo,transform,size) if transform else None
    candidates=[]
    airport_mask=masks['aerodrome'] if masks else None
    has_perimeter=airport_mask is not None and np.any(airport_mask)
    width,height=size
    for index,(kind,typ,outline) in enumerate(shapes):
        geographic=None;status='uncertain';reason='Geographic alignment required';coverage=None
        if transform:
            coords=[unproject(*mapped(transform,p)) for p in outline]
            if all(coordinate(p) for p in coords) and (typ!='Polygon' or ring_ok(coords)):
                coords=[[round(x,7),round(y,7)] for x,y in coords]
                geographic=dict(type=typ,coordinates=coords if typ=='LineString' else [coords])
                outline_px=np.round(outline).astype(np.int32)
                x,y,w,h=cv2.boundingRect(outline_px);margin=4
                x,y=max(0,x-margin),max(0,y-margin);w,h=min(width-x,w+2*margin),min(height-y,h+2*margin)
                if w<=0 or h<=0:continue
                mask=np.zeros((h,w),dtype=np.uint8)
                local=outline_px-np.array([x,y],dtype=np.int32)
                if typ=='LineString':cv2.polylines(mask,[local],False,255,3)
                else:cv2.fillPoly(mask,[local],255)
                region=mask>0;area=np.count_nonzero(region)
                if not area:continue
                def cover(k):return float(np.count_nonzero(masks[k][y:y+h,x:x+w][region]))/area
                same=max(cover(k) for k in ('building','terminal','hangar')) if kind=='building' else cover(kind)
                coverage=round(same,2)
                different=max(cover(k) for k in KINDS if k!=kind and not (kind=='building' and k in ('terminal','hangar')))
                inside=not has_perimeter or cover('aerodrome')>=.92
                if not alignment['usable']:
                    reason='Alignment error too high'
                elif same>=.70:
                    status,reason='matched','Corresponding OSM geometry'
                elif same>=.10 or different>=.35:
                    status,reason='conflict','Overlaps a mapped feature or differs from OSM'
                elif not inside or kind=='runway':
                    reason='Outside mapped airport or unmatched runway axis'
                elif not has_perimeter:
                    reason='No mapped airport perimeter to verify the candidate'
                else:
                    status,reason='proposed','Possible addition inside the airport perimeter'
            else:reason='The extracted outline is not a valid geographic shape'
        # Stable within a specific upload and page, independent of OSM overlap results.
        identity=sha256(encoded([kind,typ,[[round(x,2),round(y,2)] for x,y in outline]])).hexdigest()[:20]
        candidates.append(dict(id=identity,number=index+1,kind=kind,status=status,reason=reason,
                               coverage=coverage,outline=outline,geometry=geographic,
                               shape=sha256(encoded(geographic)).hexdigest() if geographic else None))
    return candidates


def analyze(raw, format, page, geo, controls=()):
    import cv2
    cv2.setNumThreads(1)
    cv2.setRNGSeed(0)
    image,page_count,vectors=read_diagram(raw,format,page)
    shapes=structural_shapes(image,vectors)
    if geo:transform,alignment=align(shapes,geo,image.size,controls)
    else:transform,alignment=None,{'method':'none','usable':False,'message':'Fetch the OSM source to compare geometry.'}
    candidates=classify(shapes,transform if alignment['usable'] else None,alignment,geo,image.size)
    buffer=io.BytesIO();image.save(buffer,format='PNG',optimize=True)
    return buffer.getvalue(), dict(page=page,pages=page_count,width=image.width,height=image.height,
        method='vector + raster' if vectors else 'raster', alignment=alignment,candidates=candidates)


def approved_geometry(geo, analysis, approved, reference):
    if not analysis['alignment']['usable'] or not reference['rights'] or not reference['license'] or not reference['source']:
        raise ValueError('Approval requires a usable alignment and documented redistribution rights')
    candidates={c['id']:c for c in analysis['candidates'] if c['status']=='proposed' and c['geometry']}
    additions=[]
    for identity,decision in sorted(approved.items()):
        candidate=candidates.get(identity)
        kind=decision['kind']
        if (not candidate or decision['shape']!=candidate['shape']
                or kind not in (('building','hangar','terminal') if candidate['kind']=='building' else (candidate['kind'],))):
            raise ValueError('An approved addition is stale or conflicts with OSM. Review it again.')
        additions.append(dict(type='Feature',id='diagram/'+identity,geometry=candidate['geometry'],
            properties=dict(feature=kind,source='approved airport diagram',source_id=reference['id'])))
    features=[*geo['features'],*additions]
    return {**geo,'features':features,'bounds':bounds_of(features),
        'counts':dict(Counter(f['properties']['feature'] for f in features)),
        'diagram_source':dict(source=reference['source'],license=reference['license'],edition=reference['edition'],page=analysis['page'],sha256=reference['file']['sha256'],reviewed_features=len(additions))}
