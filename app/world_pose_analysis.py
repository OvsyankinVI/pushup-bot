import math
import statistics

import cv2
import mediapipe as mp

from app.pose_analysis import _ensure_mediapipe_runtime, _ensure_model


def _midpoint(a,b): return ((a.x+b.x)/2,(a.y+b.y)/2,(a.z+b.z)/2)
def _vector(a,b): return (b[0]-a[0],b[1]-a[1],b[2]-a[2])
def _norm(v): return math.sqrt(sum(x*x for x in v))
def _angle3(a,b,c):
    ba=(a.x-b.x,a.y-b.y,a.z-b.z);bc=(c.x-b.x,c.y-b.y,c.z-b.z);d=_norm(ba)*_norm(bc)
    if d<=1e-8:return None
    return math.degrees(math.acos(max(-1.0,min(1.0,sum(x*y for x,y in zip(ba,bc))/d))))
def _median(values):
    values=[v for v in values if v is not None];return statistics.median(values) if values else None
def _percentile(values,q):
    values=sorted(v for v in values if v is not None)
    if not values:return None
    pos=(len(values)-1)*q;lo=int(math.floor(pos));hi=int(math.ceil(pos))
    if lo==hi:return values[lo]
    return values[lo]+(values[hi]-values[lo])*(pos-lo)
def _pearson(xs,ys):
    pairs=[(x,y) for x,y in zip(xs,ys) if x is not None and y is not None]
    if len(pairs)<6:return None
    ax=sum(x for x,_ in pairs)/len(pairs);ay=sum(y for _,y in pairs)/len(pairs);dx=[x-ax for x,_ in pairs];dy=[y-ay for _,y in pairs];den=math.sqrt(sum(x*x for x in dx)*sum(y*y for y in dy))
    if den<=1e-8:return None
    return sum(x*y for x,y in zip(dx,dy))/den
def _motion_coupling(elbows,shoulder_heights):
    # Correlate frame-to-frame elbow flexion with shoulder/body vertical motion.
    # Absolute value is diagnostic because world-axis sign can flip with camera/view.
    de=[];ds=[]
    for i in range(1,min(len(elbows),len(shoulder_heights))):
        if elbows[i] is None or elbows[i-1] is None or shoulder_heights[i] is None or shoulder_heights[i-1] is None:continue
        de.append(elbows[i]-elbows[i-1]);ds.append(shoulder_heights[i]-shoulder_heights[i-1])
    corr=_pearson(de,ds)
    return abs(corr) if corr is not None else None,len(de)

def _context_verticality(timeline,start,end):
    values=[timeline[i]['verticality'] for i in range(max(0,start),min(len(timeline),end)) if timeline[i] and timeline[i].get('verticality') is not None]
    return _median(values),len(values)

def _segments(mask,min_frames=3):
    mask=list(mask)
    for i in range(1,len(mask)-1):
        if not mask[i] and mask[i-1] and mask[i+1]:mask[i]=True
    result=[];start=None
    for i,value in enumerate(mask+[False]):
        if value and start is None:start=i
        elif not value and start is not None:
            if i-start>=min_frames:result.append((start,i))
            start=None
    return result

def _count_elbow_cycles(values,min_range=35.0):
    valid=[v for v in values if v is not None]
    if len(valid)<5:return 0,0.0
    ordered=sorted(valid);lo=ordered[int((len(ordered)-1)*.15)];hi=ordered[int((len(ordered)-1)*.85)];amplitude=hi-lo
    if amplitude<min_range:return 0,amplitude
    low=lo+amplitude*.30;high=lo+amplitude*.70;state=None;transitions=0
    for value in values:
        if value is None:continue
        if state is None:
            if value<=low:state='bent'
            elif value>=high:state='straight'
        elif state=='straight' and value<=low:state='bent';transitions+=1
        elif state=='bent' and value>=high:state='straight';transitions+=1
    return transitions//2,amplitude

def analyze_world_pose(video_path:str,sample_fps:float=6.0)->dict:
    _ensure_mediapipe_runtime();model=_ensure_model();capture=cv2.VideoCapture(video_path)
    if not capture.isOpened():raise ValueError('Video cannot be opened for world pose analysis')
    source_fps=capture.get(cv2.CAP_PROP_FPS) or 25.0;every_n=max(1,round(source_fps/sample_fps));actual_fps=source_fps/every_n
    sampled=world_frames=horizontal=vertical=straight=pushup_like=0;torso_verticalities=[];body_angles=[];timeline=[]
    options=mp.tasks.vision.PoseLandmarkerOptions(base_options=mp.tasks.BaseOptions(model_asset_path=model),running_mode=mp.tasks.vision.RunningMode.VIDEO,num_poses=1,min_pose_detection_confidence=.45,min_pose_presence_confidence=.45,min_tracking_confidence=.45)
    frame_index=0
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok,frame=capture.read()
                if not ok:break
                idx=frame_index;frame_index+=1
                if idx%every_n:continue
                sampled+=1;result=landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)),int(idx*1000/source_fps))
                if not result.pose_world_landmarks:timeline.append(None);continue
                world_frames+=1;lm=result.pose_world_landmarks[0];shoulder=_midpoint(lm[11],lm[12]);hip=_midpoint(lm[23],lm[24]);torso=_vector(shoulder,hip);torso_len=_norm(torso)
                if torso_len<=1e-8:timeline.append(None);continue
                verticality=abs(torso[1])/torso_len;torso_verticalities.append(verticality)
                if verticality<=.55:horizontal+=1
                if verticality>=.75:vertical+=1
                side_angles=[];elbow_angles=[]
                for s,h,a,e,w in ((11,23,27,13,15),(12,24,28,14,16)):
                    ba=_angle3(lm[s],lm[h],lm[a]);ea=_angle3(lm[s],lm[e],lm[w])
                    if ba is not None:side_angles.append(ba)
                    if ea is not None:elbow_angles.append(ea)
                body_angle=max(side_angles) if side_angles else None;elbow_angle=_median(elbow_angles);knee_angle=_median([_angle3(lm[23],lm[25],lm[27]),_angle3(lm[24],lm[26],lm[28])])
                if body_angle is not None:
                    body_angles.append(body_angle)
                    if body_angle>=145:straight+=1
                    if verticality<=.60 and body_angle>=140:pushup_like+=1
                timeline.append({'pose':verticality<=.68,'elbow':elbow_angle,'verticality':verticality,'body_angle':body_angle,'shoulder_y':shoulder[1],'hip_y':hip[1],'knee_angle':knee_angle})
    finally:capture.release()
    pose_mask=[bool(row and row['pose']) for row in timeline];segments=_segments(pose_mask,max(3,int(actual_fps*1.0)));min_counted_duration_s=2.5;min_counted_frames=max(5,int(actual_fps*min_counted_duration_s));context_window_s=3.0;context_frames=max(1,int(actual_fps*context_window_s));segment_details=[];gated_count=gated_frames=counted_segments=0
    for start,end in segments:
        rows=[timeline[i] for i in range(start,end) if timeline[i]];elbows=[r['elbow'] for r in rows];verticalities=[r['verticality'] for r in rows];shoulders=[r['shoulder_y'] for r in rows];knees=[r['knee_angle'] for r in rows];hips=[r['hip_y'] for r in rows]
        count,amplitude=_count_elbow_cycles(elbows);frames=end-start;counted=frames>=min_counted_frames;median_v=_median(verticalities);p75_v=_percentile(verticalities,.75);p90_v=_percentile(verticalities,.90);coupling,coupling_samples=_motion_coupling(elbows,shoulders)
        knee_p10=_percentile(knees,.10);knee_p90=_percentile(knees,.90);knee_range=(knee_p90-knee_p10) if knee_p10 is not None and knee_p90 is not None else None;hip_p10=_percentile(hips,.10);hip_p90=_percentile(hips,.90);hip_range=(hip_p90-hip_p10) if hip_p10 is not None and hip_p90 is not None else None;knee_elbow_sync,knee_elbow_samples=_motion_coupling(elbows,knees)
        strict_horizontal_ratio=sum(1 for v in verticalities if v is not None and v<=.55)/len(verticalities) if verticalities else 0.0;pre_v,pre_frames=_context_verticality(timeline,start-context_frames,start);post_v,post_frames=_context_verticality(timeline,end,end+context_frames)
        gated_frames+=frames
        if counted:gated_count+=count;counted_segments+=1
        segment_details.append({'start_s':round(start/actual_fps,2),'end_s':round(end/actual_fps,2),'frames':frames,'count':count,'counted':counted,'elbow_range':round(amplitude,1),'median_verticality':round(median_v,3) if median_v is not None else None,'p75_verticality':round(p75_v,3) if p75_v is not None else None,'p90_verticality':round(p90_v,3) if p90_v is not None else None,'motion_coupling':round(coupling,3) if coupling is not None else None,'coupling_samples':coupling_samples,'strict_horizontal_ratio':round(strict_horizontal_ratio,3),'pre_verticality':round(pre_v,3) if pre_v is not None else None,'post_verticality':round(post_v,3) if post_v is not None else None,'pre_context_frames':pre_frames,'post_context_frames':post_frames,'knee_range_deg':round(knee_range,1) if knee_range is not None else None,'hip_y_range':round(hip_range,3) if hip_range is not None else None,'knee_elbow_sync':round(knee_elbow_sync,3) if knee_elbow_sync is not None else None,'knee_elbow_samples':knee_elbow_samples})
    denominator=world_frames or 1
    return {'sampled_frames':sampled,'world_frames':world_frames,'world_pose_ratio':world_frames/sampled if sampled else 0.0,'horizontal_ratio':horizontal/denominator,'vertical_ratio':vertical/denominator,'straight_body_ratio':straight/denominator,'pushup_pose_ratio':pushup_like/denominator,'median_torso_verticality':_median(torso_verticalities),'median_body_line_deg':_median(body_angles),'gated_frames':gated_frames,'gated_ratio':gated_frames/sampled if sampled else 0.0,'gated_segments':len(segments),'counted_segments':counted_segments,'gated_pushup_count':gated_count,'segment_details':segment_details,'actual_sample_fps':actual_fps,'min_counted_segment_s':min_counted_duration_s,'context_window_s':context_window_s}
