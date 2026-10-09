import ctypes
import hashlib
import math
import os
import statistics
import tarfile
import tempfile
import urllib.request

import cv2
import mediapipe as mp

MODEL_URL = ('https://storage.googleapis.com/mediapipe-models/pose_landmarker/'
             'pose_landmarker_lite/float16/latest/pose_landmarker_lite.task')
MODEL_PATH = '/tmp/pose_landmarker_lite.task'
DEBIAN_POOL = 'https://ftp.debian.org/debian/pool/main/libg/libglvnd/'
RUNTIME_DIR = '/tmp/pushup_gl_runtime'
DEBS = (
    ('libglvnd0_1.6.0-1_amd64.deb', 'b6da5b153dd62d8b5e5fbe25242db1fc05c068707c365db49abda8c2427c75f8'),
    ('libgles2_1.6.0-1_amd64.deb', '07b2f51b8aa3c8d6d928133cd46087bd8793d0d67c203f09fc289d45a2cf5f47'),
)
SUPPORTING = (15, 16, 25, 26, 27, 28)
_runtime_loaded = False


def _download_checked(url, path, sha256):
    urllib.request.urlretrieve(url, path); digest=hashlib.sha256()
    with open(path,'rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''): digest.update(chunk)
    if digest.hexdigest()!=sha256: raise RuntimeError('Downloaded runtime package checksum mismatch')


def _extract_deb_data(deb_path,destination):
    with open(deb_path,'rb') as handle:
        if handle.read(8)!=b'!<arch>\n': raise RuntimeError('Invalid Debian package archive')
        while True:
            header=handle.read(60)
            if not header: break
            name=header[:16].decode('ascii').strip().rstrip('/'); size=int(header[48:58].decode('ascii').strip()); payload=handle.read(size)
            if size%2: handle.read(1)
            if name.startswith('data.tar'):
                with tempfile.NamedTemporaryFile(suffix=name[len('data.tar'):]) as f:
                    f.write(payload); f.flush()
                    with tarfile.open(f.name,mode='r:*') as archive: archive.extractall(destination,filter='data')
                return
    raise RuntimeError('Debian package has no data archive')


def _ensure_mediapipe_runtime():
    global _runtime_loaded
    if _runtime_loaded:return
    lib_dir=os.path.join(RUNTIME_DIR,'usr','lib','x86_64-linux-gnu'); gles=os.path.join(lib_dir,'libGLESv2.so.2'); dispatch=os.path.join(lib_dir,'libGLdispatch.so.0')
    if not os.path.isfile(gles) or not os.path.isfile(dispatch):
        os.makedirs(RUNTIME_DIR,exist_ok=True)
        for filename,checksum in DEBS:
            deb_path=os.path.join('/tmp',filename); _download_checked(DEBIAN_POOL+filename,deb_path,checksum)
            try:_extract_deb_data(deb_path,RUNTIME_DIR)
            finally:
                try:os.remove(deb_path)
                except OSError:pass
    ctypes.CDLL(dispatch,mode=ctypes.RTLD_GLOBAL); ctypes.CDLL(gles,mode=ctypes.RTLD_GLOBAL); _runtime_loaded=True


def _ensure_model():
    if os.path.isfile(MODEL_PATH) and os.path.getsize(MODEL_PATH)>0:return MODEL_PATH
    tmp=MODEL_PATH+'.download'
    try:urllib.request.urlretrieve(MODEL_URL,tmp);os.replace(tmp,MODEL_PATH)
    finally:
        if os.path.exists(tmp):os.remove(tmp)
    return MODEL_PATH


def _distance(a,b):return math.hypot(a.x-b.x,a.y-b.y)
def _angle(a,b,c):
    ab=(a.x-b.x,a.y-b.y);cb=(c.x-b.x,c.y-b.y);denom=math.hypot(*ab)*math.hypot(*cb)
    if denom<=1e-8:return None
    return math.degrees(math.acos(max(-1.0,min(1.0,(ab[0]*cb[0]+ab[1]*cb[1])/denom))))


def _smooth(values,radius=2):
    out=[]
    for i in range(len(values)):
        window=[v for v in values[max(0,i-radius):i+radius+1] if v is not None];out.append(statistics.median(window) if window else None)
    return out


def _count_cycles(values,fps,min_amplitude):
    valid=[v for v in values if v is not None]
    if len(valid)<max(8,int(fps*2)):return 0,0.0,0.0
    ordered=sorted(valid);lo=ordered[int((len(ordered)-1)*.12)];hi=ordered[int((len(ordered)-1)*.88)];amp=hi-lo
    if amp<min_amplitude:return 0,amp,0.0
    low=lo+amp*.32;high=lo+amp*.68;state=None;count=transitions=0;last=-999;gap=max(1,int(fps*.22))
    for i,v in enumerate(values):
        if v is None:continue
        if state is None:
            if v>=high:state='high'
            elif v<=low:state='low'
        elif state=='high' and v<=low and i-last>=gap:state='low';transitions+=1;last=i
        elif state=='low' and v>=high and i-last>=gap:state='high';transitions+=1;count+=1;last=i
    coverage=len(valid)/len(values);quality=coverage*min(1.0,transitions/max(2,count*2)) if count else 0.0
    return count,amp,quality


def _count_local_cycles(values,fps,min_amplitude):
    smooth=_smooth(values,radius=1);valid=[v for v in smooth if v is not None]
    if len(valid)<max(8,int(fps*2)):return 0,0.0,0.0
    ordered=sorted(valid);global_amp=ordered[int((len(ordered)-1)*.90)]-ordered[int((len(ordered)-1)*.10)]
    if global_amp<min_amplitude:return 0,global_amp,0.0
    prominence=max(min_amplitude*.55,global_amp*.16);min_sep=max(2,int(fps*.28));extrema=[]
    for i in range(1,len(smooth)-1):
        a,b,c=smooth[i-1],smooth[i],smooth[i+1]
        if a is None or b is None or c is None:continue
        kind='high' if b>=a and b>c else ('low' if b<=a and b<c else None)
        if not kind:continue
        if extrema and i-extrema[-1][0]<min_sep and extrema[-1][1]==kind:
            better=(kind=='high' and b>extrema[-1][2]) or (kind=='low' and b<extrema[-1][2])
            if better:extrema[-1]=(i,kind,b)
            continue
        if extrema and extrema[-1][1]==kind:
            better=(kind=='high' and b>extrema[-1][2]) or (kind=='low' and b<extrema[-1][2])
            if better:extrema[-1]=(i,kind,b)
        else:extrema.append((i,kind,b))
    strong=[]
    for e in extrema:
        if not strong:strong.append(e);continue
        if e[1]!=strong[-1][1] and abs(e[2]-strong[-1][2])>=prominence:strong.append(e)
        elif e[1]==strong[-1][1]:
            better=(e[1]=='high' and e[2]>strong[-1][2]) or (e[1]=='low' and e[2]<strong[-1][2])
            if better:strong[-1]=e
    transitions=max(0,len(strong)-1);count=transitions//2
    coverage=len(valid)/len(values);quality=coverage*min(1.0,transitions/max(2,count*2)) if count else 0.0
    return count,global_amp,quality


def _choose_consensus(candidates,dominant_side=None):
    if dominant_side:
        elbow=next((c for c in candidates if c['name']==f'{dominant_side}_elbow_y'),None);angle=next((c for c in candidates if c['name']==f'{dominant_side}_angle'),None)
        viable=[c for c in (elbow,angle) if c and c['count']>0 and c['quality']>=.35]
        if viable:
            chosen=max(viable,key=lambda c:(c['count'],c['quality']));agreement=sum(c['quality'] for c in viable if abs(c['count']-chosen['count'])<=2)/sum(c['quality'] for c in viable)
            return chosen['count'],chosen['name'],agreement
    active=[c for c in candidates if c['count']>0 and c['quality']>=.30]
    if not active:
        side=[c for c in candidates if c['count']>0 and c['quality']>=.15 and c['name']!='body_y']
        if side:
            chosen=max(side,key=lambda c:(c['quality'],c['count']));return chosen['count'],chosen['name'],chosen['quality']
        return 0,'none',0.0
    active.sort(key=lambda c:c['count']);total=sum(c['quality'] for c in active);running=0.0;chosen=active[-1]
    for c in active:
        running+=c['quality']
        if running>=total/2:chosen=c;break
    agreement=sum(c['quality'] for c in active if abs(c['count']-chosen['count'])<=1)/total
    return chosen['count'],chosen['name'],agreement


def _front_view_cycles(values, fps, min_amplitude=.04):
    """Per-cycle local peaks, avoiding one global threshold across a long clip."""
    smooth=_smooth(values,radius=1)
    valid=[v for v in smooth if v is not None]
    if len(valid)<max(8,int(fps*2)):return {'count':0,'timestamps':[],'amplitude':0.0}
    lo,hi=sorted(valid)[int((len(valid)-1)*.1)],sorted(valid)[int((len(valid)-1)*.9)]
    amplitude=hi-lo
    if amplitude<min_amplitude:return {'count':0,'timestamps':[],'amplitude':round(amplitude,3)}
    min_sep=max(2,round(fps*.38))
    prominence=max(min_amplitude*.7,amplitude*.12)
    extrema=[]
    for i in range(1,len(smooth)-1):
        a,b,c=smooth[i-1:i+2]
        if a is None or b is None or c is None:continue
        kind='peak' if b>=a and b>c else ('valley' if b<=a and b<c else None)
        if kind is None:continue
        if extrema and kind==extrema[-1][1]:
            old=extrema[-1]
            if (kind=='peak' and b>old[2]) or (kind=='valley' and b<old[2]):extrema[-1]=(i,kind,b)
            continue
        extrema.append((i,kind,b))
    accepted=[]; details=[]
    for j in range(1,len(extrema)-1):
        before,mid,after=extrema[j-1:j+2]
        if mid[1]!='peak' or before[1]!='valley' or after[1]!='valley':continue
        if min(mid[2]-before[2],mid[2]-after[2])<prominence:continue
        if accepted and mid[0]-accepted[-1]<min_sep:continue
        accepted.append(mid[0])
        details.append({'time':round(mid[0]/fps,2),'start':round(before[0]/fps,2),'end':round(after[0]/fps,2),'rise':round(mid[2]-before[2],3),'fall':round(mid[2]-after[2],3)})
    return {'count':len(accepted),'timestamps':[round(i/fps,2) for i in accepted],'amplitude':round(amplitude,3),'details':details}


def _full_cycle_shadow(angle_values, shoulder_values, fps):
    """Experimental complete bend/extend transitions, never used for scoring.

    Adaptive local angle range prevents a single global threshold from
    dominating clips with changing camera geometry. Shoulder movement is
    supporting evidence only; it is not a hard gate across camera angles.
    """
    angle=_smooth(angle_values, radius=2)
    shoulder=_smooth(shoulder_values, radius=2)
    n=len(angle)
    if n<max(12,int(fps*3)):
        return {'count':0,'timestamps':[],'candidates':[],'mode':'diagnostic_only'}
    window=max(12,round(fps*7))
    phase='seek_extended'
    bent_at=None
    bent_angle=None
    last_count=-100000
    candidates=[]
    for i,v in enumerate(angle):
        if v is None:continue
        local=sorted(x for x in angle[max(0,i-window//2):min(n,i+window//2+1)] if x is not None)
        if len(local)<max(8,round(fps*2)):continue
        low=local[int((len(local)-1)*.20)]
        high=local[int((len(local)-1)*.80)]
        span=high-low
        if span<12.0:continue
        lower=low+.38*span
        upper=low+.65*span
        if phase=='seek_extended':
            if v>=upper:phase='seek_bent'
        elif phase=='seek_bent':
            if v<=lower:
                bent_at=i
                bent_angle=v
                phase='seek_return'
        elif phase=='seek_return':
            if v<bent_angle:
                bent_angle=v
                bent_at=i
            if v>=upper:
                if bent_at is not None and i-bent_at>=max(1,round(fps*.17)) and i-last_count>=max(2,round(fps*.45)):
                    lo=max(0,bent_at-round(fps*.6))
                    hi=min(n,i+1)
                    valid=[shoulder[j] for j in range(lo,hi) if shoulder[j] is not None]
                    shoulder_range=max(valid)-min(valid) if len(valid)>=3 else None
                    candidates.append({'time':round(i/fps,2),'bent_at':round(bent_at/fps,2),
                                       'angle_depth':round(v-bent_angle,1),
                                       'shoulder_range':round(shoulder_range,3) if shoulder_range is not None else None})
                    last_count=i
                phase='seek_bent'
                bent_at=None
                bent_angle=None
    return {'count':len(candidates),'timestamps':[x['time'] for x in candidates],
            'candidates':candidates,'mode':'diagnostic_only'}


def analyze_pose_visibility(video_path:str,sample_fps:float=6.0, world_result_consumer=None)->dict:
    _ensure_mediapipe_runtime();model=_ensure_model();capture=cv2.VideoCapture(video_path)
    if not capture.isOpened():raise ValueError('Video cannot be opened by OpenCV')
    source_fps=capture.get(cv2.CAP_PROP_FPS) or 25.0;every_n=max(1,round(source_fps/sample_fps));actual_fps=source_fps/every_n
    total=sampled=pose_frames=usable=0;visibility_sum=supporting_sum=0.0;left_score=right_score=0.0;left_frames=right_frames=0
    geometry_frames=horizontal_frames=vertical_frames=straight_frames=pushup_like_frames=0
    world_results=[]
    torso_tilts=[];body_line_angles=[]
    signals={n:[] for n in ('body_y','left_elbow_y','right_elbow_y','left_angle','right_angle','left_shoulder_y','right_shoulder_y')}
    options=mp.tasks.vision.PoseLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path=model),running_mode=mp.tasks.vision.RunningMode.VIDEO,num_poses=1,min_pose_detection_confidence=.45,min_pose_presence_confidence=.45,min_tracking_confidence=.45)
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok,frame=capture.read()
                if not ok:break
                idx=total;total+=1
                if idx%every_n:continue
                sampled+=1;result=landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)),int(idx*1000/source_fps))
                if world_result_consumer is not None: world_results.append(result.pose_world_landmarks[0] if result.pose_world_landmarks else None)
                if not result.pose_landmarks:
                    for v in signals.values():v.append(None)
                    continue
                pose_frames+=1;lm=result.pose_landmarks[0];lc=[lm[i].visibility for i in (11,13,23)];rc=[lm[i].visibility for i in (12,14,24)];lmmean=sum(lc)/3;rmmean=sum(rc)/3
                visibility_sum+=max(lmmean,rmmean);supporting=[lm[i].visibility for i in SUPPORTING];supporting_sum+=sum(supporting)/len(supporting);lok=min(lc)>=.25 and lmmean>=.45;rok=min(rc)>=.25 and rmmean>=.45
                if lok:left_score+=lmmean;left_frames+=1
                if rok:right_score+=rmmean;right_frames+=1
                if not lok and not rok:
                    for v in signals.values():v.append(None)
                    continue
                usable+=1;parts=[]
                if lok:parts.append(_distance(lm[11],lm[23]))
                if rok:parts.append(_distance(lm[12],lm[24]))
                torso=max(sum(parts)/len(parts),.05)
                if lok and rok:sy=(lm[11].y+lm[12].y)/2;hy=(lm[23].y+lm[24].y)/2;sx=(lm[11].x+lm[12].x)/2;hx=(lm[23].x+lm[24].x)/2
                else:s,h=(11,23) if lok else (12,24);sy=lm[s].y;hy=lm[h].y;sx=lm[s].x;hx=lm[h].x
                signals['left_shoulder_y'].append(lm[11].y/torso if lok else None);signals['right_shoulder_y'].append(lm[12].y/torso if rok else None)
                signals['body_y'].append((sy+hy)/(2*torso));signals['left_elbow_y'].append((lm[13].y-lm[11].y)/torso if lok else None);signals['right_elbow_y'].append((lm[14].y-lm[12].y)/torso if rok else None)
                for name,s,e,w,okside in (('left_angle',11,13,15,lok),('right_angle',12,14,16,rok)):signals[name].append(_angle(lm[s],lm[e],lm[w]) if okside and min(lm[s].visibility,lm[e].visibility,lm[w].visibility)>=.25 else None)

                # Geometry diagnostics: orientation is intentionally rotation-independent
                # in image coordinates and does not require wrists/hands.
                geometry_frames+=1
                torso_tilt=math.degrees(math.atan2(abs(hy-sy),max(abs(hx-sx),1e-6)))
                torso_tilts.append(torso_tilt)
                if torso_tilt<=45:horizontal_frames+=1
                if torso_tilt>=60:vertical_frames+=1
                side_angles=[]
                for shoulder,hip,ankle,okside in ((11,23,27,lok),(12,24,28,rok)):
                    if not okside or lm[ankle].visibility<.20:continue
                    body_angle=_angle(lm[shoulder],lm[hip],lm[ankle])
                    if body_angle is not None:side_angles.append(body_angle)
                if side_angles:
                    body_line=max(side_angles);body_line_angles.append(body_line)
                    if body_line>=145:straight_frames+=1
                    if torso_tilt<=50 and body_line>=140:pushup_like_frames+=1
    finally:capture.release()
    if sampled==0:raise ValueError('Video contains no readable sampled frames')
    lavg=left_score/left_frames if left_frames else 0;ravg=right_score/right_frames if right_frames else 0;lcov=left_frames/sampled;rcov=right_frames/sampled;dominant_side=None
    if lcov>=.55 and (lcov-rcov>=.18 or lavg-ravg>=.15):dominant_side='left'
    elif rcov>=.55 and (rcov-lcov>=.18 or ravg-lavg>=.15):dominant_side='right'
    candidates=[];thresholds={'body_y':.05,'left_elbow_y':.04,'right_elbow_y':.04,'left_angle':18.0,'right_angle':18.0}
    for name,values in signals.items():
        if name.endswith('shoulder_y'):continue
        if dominant_side and name in (f'{dominant_side}_elbow_y',f'{dominant_side}_angle'):count,amp,quality=_count_local_cycles(values,actual_fps,thresholds[name])
        else:count,amp,quality=_count_cycles(_smooth(values),actual_fps,thresholds[name])
        candidates.append({'name':name,'count':count,'amplitude':round(amp,3),'quality':round(quality,3)})
    count,selected,agreement=_choose_consensus(candidates,dominant_side)
    front_cycles={side:_front_view_cycles(signals[f'{side}_elbow_y'],actual_fps) for side in ('left','right')}
    # Diagnostic-only local elbow-angle cycle candidates. Keep the existing
    # 3D and paired 2D classification paths completely unchanged.
    angle_shadow={}
    for side in ('left','right'):
        info=_front_view_cycles(signals[f'{side}_angle'],actual_fps,min_amplitude=10.0)
        angle_shadow[side]=info
    angle_counts=[angle_shadow[side]['count'] for side in ('left','right')]
    angle_shadow['suggested']=min(angle_counts) if all(n>=3 for n in angle_counts) else max(angle_counts)
    angle_shadow['mode']='diagnostic_only'
    full_cycle_shadow={side:_full_cycle_shadow(signals[f'{side}_angle'],
                                                signals[f'{side}_shoulder_y'],actual_fps)
                       for side in ('left','right')}
    full_cycle_shadow['mode']='diagnostic_only'
    # Two-arm peaks must represent the same repetition, not independent noise.
    left=front_cycles['left']['details'];right=front_cycles['right']['details']
    pairs=[];used=set()
    for l in left:
        options=[(abs(l['time']-rr['time']),j,rr) for j,rr in enumerate(right) if j not in used and abs(l['time']-rr['time'])<=.30]
        if not options:continue
        _,j,rr=min(options,key=lambda item:item[0]);used.add(j)
        pairs.append({'time':round((l['time']+rr['time'])/2,2),'left':l,'right':rr})
    front_cycles['paired']={'count':len(pairs),'details':pairs}
    # Shadow-only experiment: single-arm evidence is never used by the
    # classifier. Compare it with the current paired/3D decisions first.
    candidate_by_name={item['name']:item for item in candidates}
    shadow=[]
    for side in ('left','right'):
        cycles=front_cycles[side]
        details=cycles.get('details') or []
        strengths=[min(item['rise'],item['fall']) for item in details]
        median_strength=statistics.median(strengths) if strengths else 0.0
        angle=candidate_by_name[f'{side}_angle']
        coverage=lcov if side=='left' else rcov
        mean_visibility=lavg if side=='left' else ravg
        # This is a candidate, not proof of a push-up. Count is diagnostic.
        eligible=(coverage>=.70 and mean_visibility>=.65
                  and cycles['count']>=3 and median_strength>=.10
                  and angle['count']>=3 and angle['amplitude']>=18.0)
        shadow.append({'side':side,'count':cycles['count'],
                       'coverage':round(coverage,3),
                       'visibility':round(mean_visibility,3),
                       'median_strength':round(median_strength,3),
                       'angle_cycles':angle['count'],
                       'angle_amplitude':angle['amplitude'],
                       'eligible':eligible})
    shadow.sort(key=lambda item:(item['eligible'],item['coverage'],
                                  item['median_strength'],item['count']),reverse=True)
    single_arm_shadow={'suggested_count':shadow[0]['count'] if shadow and shadow[0]['eligible'] else 0,
                       'suggested_side':shadow[0]['side'] if shadow and shadow[0]['eligible'] else 'none',
                       'candidates':shadow,'mode':'diagnostic_only'}
    # Read-only diagnostic: is each elbow cycle accompanied by shoulder
    # displacement in the same phase? Not used in classification.
    motion_consistency={}
    for side in ('left','right'):
        shoulder=_smooth(signals[f'{side}_shoulder_y'],radius=2)
        elbow=_smooth(signals[f'{side}_elbow_y'],radius=2)
        samples=[]
        for cycle in front_cycles[side].get('details',[]):
            start=max(0,round(cycle['start']*actual_fps))
            peak=min(len(shoulder)-1,round(cycle['time']*actual_fps))
            end=min(len(shoulder)-1,round(cycle['end']*actual_fps))
            if start>=peak or peak>=end:continue
            s0,s1,s2=shoulder[start],shoulder[peak],shoulder[end]
            e0,e1,e2=elbow[start],elbow[peak],elbow[end]
            if any(v is None for v in (s0,s1,s2,e0,e1,e2)):continue
            erise=e1-e0;efall=e2-e1
            srise=s1-s0;sfall=s2-s1
            # Both signals are normalized by torso size. Preserve signed
            # movement and report correlation, not just a hard pass/fail.
            direction_agreement=(erise*srise>0 and efall*sfall>0)
            shoulder_excursion=min(abs(srise),abs(sfall))
            elbow_excursion=min(abs(erise),abs(efall))
            samples.append({'time':cycle['time'],
                            'shoulder_excursion':round(shoulder_excursion,3),
                            'elbow_excursion':round(elbow_excursion,3),
                            'phase_agree':direction_agreement,
                            'relative_shoulder_motion':round(shoulder_excursion/elbow_excursion,3) if elbow_excursion>=.03 else None})
        agreements=sum(bool(s['phase_agree']) for s in samples)
        motion_consistency[side]={
            'evaluated':len(samples),'phase_agreement_ratio':round(agreements/len(samples),3) if samples else None,
            'median_shoulder_excursion':round(statistics.median(s['shoulder_excursion'] for s in samples),3) if samples else None,
            'cycles':samples,'mode':'diagnostic_only'}
    # Per-repetition validation is diagnostic only: no classification changes.
    repetition_validation={}
    for side in ('left','right'):
        elbow_details=front_cycles[side].get('details',[])
        angle_times=angle_shadow[side].get('timestamps',[])
        full_times=full_cycle_shadow[side].get('timestamps',[])
        shoulder=_smooth(signals[f'{side}_shoulder_y'],radius=2)
        intervals=[b['time']-a['time'] for a,b in zip(elbow_details,elbow_details[1:])]
        median_interval=statistics.median(intervals) if intervals else None
        strengths=[min(c['rise'],c['fall']) for c in elbow_details]
        median_strength=statistics.median(strengths) if strengths else None
        accepted=uncertain=rejected=0
        reasons={'no_angle':0,'weak_shoulder':0,'irregular_timing':0,'weak_amplitude':0}
        examples=[]
        for idx,c in enumerate(elbow_details):
            center=c['time']
            # Angle minima and elbow-position maxima can be out of phase.
            # Match within the candidate's full start/end interval, capped
            # to avoid claiming evidence from an unrelated repetition.
            half_window=min(0.75,max(0.35,(c['end']-c['start'])/2))
            angle_ok=any(abs(t-center)<=half_window for t in angle_times)
            full_ok=any(abs(t-center)<=half_window for t in full_times)
            start=max(0,round(c['start']*actual_fps))
            end=min(len(shoulder),round(c['end']*actual_fps)+1)
            shoulder_valid=[v for v in shoulder[start:end] if v is not None]
            shoulder_range=max(shoulder_valid)-min(shoulder_valid) if len(shoulder_valid)>=3 else None
            shoulder_ok=shoulder_range is not None and shoulder_range>=.12
            amplitude_ok=median_strength is not None and min(c['rise'],c['fall'])>=max(.04,median_strength*.45)
            gap=center-elbow_details[idx-1]['time'] if idx else None
            timing_ok=gap is None or median_interval is None or .5*median_interval<=gap<=2.5*median_interval
            # Shoulder displacement is supporting evidence, not mandatory:
            # normalized coordinates are camera-dependent.
            evidence=int(angle_ok)+int(full_ok)+int(shoulder_ok)+int(amplitude_ok)+int(timing_ok)
            if evidence>=4 and (angle_ok or full_ok):label='confirmed';accepted+=1
            elif evidence>=2:label='uncertain';uncertain+=1
            else:label='rejected';rejected+=1
            if not angle_ok:reasons['no_angle']+=1
            if not shoulder_ok:reasons['weak_shoulder']+=1
            if not timing_ok:reasons['irregular_timing']+=1
            if not amplitude_ok:reasons['weak_amplitude']+=1
            if label!='confirmed' and len(examples)<5:examples.append({'time':center,'label':label,'evidence':evidence})
        repetition_validation[side]={'candidates':len(elbow_details),'confirmed':accepted,
                                    'uncertain':uncertain,'rejected':rejected,
                                    'missing_evidence':reasons,'examples':examples,
                                    'mode':'diagnostic_only'}
    repetition_validation['mode']='diagnostic_only'
    # Diagnostic-only signal reliability. These scores rank signal quality,
    # NOT the probability that a candidate is a real push-up.
    signal_reliability={}
    for side in ('left','right'):
        cycles=front_cycles[side].get('details',[])
        times=[c['time'] for c in cycles]
        intervals=[b-a for a,b in zip(times,times[1:]) if b>a]
        strengths=[min(c['rise'],c['fall']) for c in cycles]
        coverage=lcov if side=='left' else rcov
        visibility=lavg if side=='left' else ravg
        angle_n=angle_shadow[side]['count']
        front_n=len(cycles)
        full_n=full_cycle_shadow[side]['count']
        if len(intervals)>=3:
            med_interval=statistics.median(intervals)
            regularity=sum(abs(v-med_interval)<=max(.25,med_interval*.35) for v in intervals)/len(intervals)
        else:
            med_interval=None
            regularity=None
        if len(strengths)>=3:
            med_strength=statistics.median(strengths)
            amplitude_stability=sum(.5*med_strength<=v<=2*med_strength for v in strengths)/len(strengths) if med_strength>0 else None
        else:
            med_strength=None
            amplitude_stability=None
        # Relative agreement is symmetric; a zero-count method does not
        # silently become supporting evidence for a positive count.
        def count_agreement(other):
            if not front_n or not other:return None
            return round(min(front_n,other)/max(front_n,other),3)
        paired_n=sum(1 for p in pairs if p['left']['time'] in times or p['right']['time'] in times)
        components={'coverage':coverage,'visibility':visibility,
                    'interval_regularity':regularity,'amplitude_stability':amplitude_stability}
        available=[v for v in components.values() if v is not None]
        score=round(sum(available)/len(available),3) if available else None
        signal_reliability[side]={
            'score':score,'components':{k:round(v,3) if v is not None else None for k,v in components.items()},
            'front_count':front_n,'angle_count':angle_n,'full_cycle_count':full_n,
            'paired_count':paired_n,'angle_agreement':count_agreement(angle_n),
            'full_cycle_agreement':count_agreement(full_n),
            'median_interval':round(med_interval,3) if med_interval is not None else None,
            'median_strength':round(med_strength,3) if med_strength is not None else None,
            'mode':'diagnostic_only'}
    signal_reliability['mode']='diagnostic_only'
    geometry={
        'frames':geometry_frames,
        'horizontal_ratio':horizontal_frames/geometry_frames if geometry_frames else 0.0,
        'vertical_ratio':vertical_frames/geometry_frames if geometry_frames else 0.0,
        'straight_body_ratio':straight_frames/geometry_frames if geometry_frames else 0.0,
        'pushup_pose_ratio':pushup_like_frames/geometry_frames if geometry_frames else 0.0,
        'median_torso_tilt_deg':statistics.median(torso_tilts) if torso_tilts else None,
        'median_body_line_deg':statistics.median(body_line_angles) if body_line_angles else None,
    }
    output={'total_frames':total,'sampled_frames':sampled,'pose_frames':pose_frames,'usable_frames':usable,'pose_ratio':pose_frames/sampled,'usable_ratio':usable/sampled,'mean_visibility':visibility_sum/pose_frames if pose_frames else 0.0,'supporting_visibility':supporting_sum/pose_frames if pose_frames else 0.0,'pushup_count':count,'selected_signal':selected,'signal_agreement':agreement,'candidates':candidates,'front_cycles':front_cycles,'angle_shadow':angle_shadow,'full_cycle_shadow':full_cycle_shadow,'single_arm_shadow':single_arm_shadow,'motion_consistency':motion_consistency,'signal_reliability':signal_reliability,'repetition_validation':repetition_validation,'dominant_side':dominant_side or 'balanced','motion_amplitude':next(c['amplitude'] for c in candidates if c['name']=='body_y'),'elbow_angle_range':max((c['amplitude'] for c in candidates if 'angle' in c['name']),default=0.0),'actual_sample_fps':actual_fps,'geometry':geometry}
    if world_result_consumer is not None: output['world_geometry']=world_result_consumer(world_results,actual_fps)
    return output
