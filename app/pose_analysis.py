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
    """Dominant-side peak/trough detector. One complete A-B-A sequence is one repetition."""
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
    # The previous implementation counted every overlapping A-B-A window, so
    # high-low-high-low-high produced 3 counts instead of 2 full repetitions.
    # A complete repetition consumes two phase transitions; count non-overlapping
    # pairs of transitions regardless of whether the clip starts at high or low.
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


def analyze_pose_visibility(video_path:str,sample_fps:float=6.0)->dict:
    _ensure_mediapipe_runtime();model=_ensure_model();capture=cv2.VideoCapture(video_path)
    if not capture.isOpened():raise ValueError('Video cannot be opened by OpenCV')
    source_fps=capture.get(cv2.CAP_PROP_FPS) or 25.0;every_n=max(1,round(source_fps/sample_fps));actual_fps=source_fps/every_n
    total=sampled=pose_frames=usable=0;visibility_sum=supporting_sum=0.0;left_score=right_score=0.0;left_frames=right_frames=0
    signals={n:[] for n in ('body_y','left_elbow_y','right_elbow_y','left_angle','right_angle')}
    options=mp.tasks.vision.PoseLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path=model),running_mode=mp.tasks.vision.RunningMode.VIDEO,num_poses=1,min_pose_detection_confidence=.45,min_pose_presence_confidence=.45,min_tracking_confidence=.45)
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok,frame=capture.read()
                if not ok:break
                idx=total;total+=1
                if idx%every_n:continue
                sampled+=1;result=landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)),int(idx*1000/source_fps))
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
                if lok and rok:sy=(lm[11].y+lm[12].y)/2;hy=(lm[23].y+lm[24].y)/2
                else:s,h=(11,23) if lok else (12,24);sy=lm[s].y;hy=lm[h].y
                signals['body_y'].append((sy+hy)/(2*torso));signals['left_elbow_y'].append((lm[13].y-lm[11].y)/torso if lok else None);signals['right_elbow_y'].append((lm[14].y-lm[12].y)/torso if rok else None)
                for name,s,e,w,okside in (('left_angle',11,13,15,lok),('right_angle',12,14,16,rok)):signals[name].append(_angle(lm[s],lm[e],lm[w]) if okside and min(lm[s].visibility,lm[e].visibility,lm[w].visibility)>=.25 else None)
    finally:capture.release()
    if sampled==0:raise ValueError('Video contains no readable sampled frames')
    lavg=left_score/left_frames if left_frames else 0;ravg=right_score/right_frames if right_frames else 0;lcov=left_frames/sampled;rcov=right_frames/sampled;dominant_side=None
    if lcov>=.55 and (lcov-rcov>=.18 or lavg-ravg>=.15):dominant_side='left'
    elif rcov>=.55 and (rcov-lcov>=.18 or ravg-lavg>=.15):dominant_side='right'
    candidates=[];thresholds={'body_y':.05,'left_elbow_y':.04,'right_elbow_y':.04,'left_angle':18.0,'right_angle':18.0}
    for name,values in signals.items():
        if dominant_side and name in (f'{dominant_side}_elbow_y',f'{dominant_side}_angle'):count,amp,quality=_count_local_cycles(values,actual_fps,thresholds[name])
        else:count,amp,quality=_count_cycles(_smooth(values),actual_fps,thresholds[name])
        candidates.append({'name':name,'count':count,'amplitude':round(amp,3),'quality':round(quality,3)})
    count,selected,agreement=_choose_consensus(candidates,dominant_side)
    return {'total_frames':total,'sampled_frames':sampled,'pose_frames':pose_frames,'usable_frames':usable,'pose_ratio':pose_frames/sampled,'usable_ratio':usable/sampled,'mean_visibility':visibility_sum/pose_frames if pose_frames else 0.0,'supporting_visibility':supporting_sum/pose_frames if pose_frames else 0.0,'pushup_count':count,'selected_signal':selected,'signal_agreement':agreement,'candidates':candidates,'dominant_side':dominant_side or 'balanced','motion_amplitude':next(c['amplitude'] for c in candidates if c['name']=='body_y'),'elbow_angle_range':max((c['amplitude'] for c in candidates if 'angle' in c['name']),default=0.0),'actual_sample_fps':actual_fps}
