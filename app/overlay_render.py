"""Staging-only OpenCV overlay renderer. Does not run MediaPipe or infer reps."""
import json
import sys
import cv2
import numpy as np

EDGES=((11,13),(13,15),(12,14),(14,16))
def main():
    source,target,trace_file=sys.argv[1:4]
    with open(trace_file,encoding='utf-8') as f: data=json.load(f)
    trace=data['trace']; decision=data['classification']
    frames=trace['frames']; sample_fps=float(trace['fps'])
    segments=(decision.get('world_geometry') or {}).get('segment_details') or []
    events=sorted(float(t) for seg in segments if seg.get('counted') for t in (seg.get('rep_event_s') or []))
    final_count=int(decision.get('count') or 0)
    # Only show a growing counter when exact algorithm events explain the final count.
    live_counter=(decision.get('status')=='accepted' and len(events)==final_count and final_count>0)
    cap=cv2.VideoCapture(source)
    if not cap.isOpened(): raise RuntimeError('Cannot open source video')
    fps=cap.get(cv2.CAP_PROP_FPS) or 25.0
    w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if w<1 or h<1: raise RuntimeError('Invalid video dimensions')
    scale=min(1.0,480.0/max(w,h))
    width=max(2,int(w*scale)//2*2);height=max(2,int(h*scale)//2*2)
    panel_height=max(128,int(height*.36)//2*2)
    canvas_height=height+panel_height
    out=cv2.VideoWriter(target,cv2.VideoWriter_fourcc(*'mp4v'),min(fps,15.0),(width,canvas_height))
    if not out.isOpened():raise RuntimeError('Cannot open MP4 writer')
    frame_index=0;written=0
    step=max(1,round(fps/15.0))
    try:
        while True:
            ok,frame=cap.read()
            if not ok:break
            if frame_index%step:
                frame_index+=1;continue
            frame=cv2.resize(frame,(width,height))
            t=frame_index/fps
            sample_idx=min(len(frames)-1,max(0,int(round(t*sample_fps)))) if frames else -1
            joints=frames[sample_idx] if sample_idx>=0 else None
            if joints:
                for a,b in EDGES:
                    pa=joints.get(str(a));pb=joints.get(str(b))
                    if pa and pb:
                        cv2.line(frame,(int(pa[0]*width),int(pa[1]*height)),(int(pb[0]*width),int(pb[1]*height)),(0,220,255),2,cv2.LINE_AA)
                for v in joints.values():
                    cv2.circle(frame,(int(v[0]*width),int(v[1]*height)),4,(0,255,80),-1,cv2.LINE_AA)
            canvas=np.empty((canvas_height,width,3),dtype=np.uint8)
            canvas[:height]=frame
            canvas[height:]=(31,24,23)
            current=sum(event<=t for event in events) if live_counter else final_count
            cv2.line(canvas,(0,height),(width,height),(75,63,54),2,cv2.LINE_AA)
            font=cv2.FONT_HERSHEY_SIMPLEX
            heading='PUSH-UPS'
            hscale=max(.55,min(.95,width/440))
            (tw,_),_=cv2.getTextSize(heading,font,hscale,2)
            cv2.putText(canvas,heading,((width-tw)//2,height+int(panel_height*.25)),font,hscale,(245,245,245),2,cv2.LINE_AA)
            number=str(current)
            nscale=max(1.5,min(3.8,width/130))
            (nw,_),_=cv2.getTextSize(number,font,nscale,5)
            cv2.putText(canvas,number,((width-nw)//2,height+int(panel_height*.77)),font,nscale,(255,255,255),5,cv2.LINE_AA)
            if not live_counter:
                note='FINAL RESULT'
                (ntw,_),_=cv2.getTextSize(note,font,.36,1)
                cv2.putText(canvas,note,((width-ntw)//2,canvas_height-10),font,.36,(180,180,180),1,cv2.LINE_AA)
            out.write(canvas);written+=1;frame_index+=1
    finally:
        out.release();cap.release()
    if written==0:raise RuntimeError('No frames rendered')
    print(json.dumps({'frames':written,'size':[width,canvas_height]}))
if __name__=='__main__':main()
