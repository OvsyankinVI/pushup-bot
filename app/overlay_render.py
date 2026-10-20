"""Staging-only OpenCV overlay renderer. Does not run MediaPipe or infer reps."""
import json
import sys
import cv2

EDGES=((11,13),(13,15),(12,14),(14,16))
def main():
    source,target,trace_file=sys.argv[1:4]
    with open(trace_file,encoding='utf-8') as f: data=json.load(f)
    trace=data['trace']; decision=data['classification']
    frames=trace['frames']; sample_fps=float(trace['fps'])
    cap=cv2.VideoCapture(source)
    if not cap.isOpened(): raise RuntimeError('Cannot open source video')
    fps=cap.get(cv2.CAP_PROP_FPS) or 25.0
    w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if w<1 or h<1: raise RuntimeError('Invalid video dimensions')
    scale=min(1.0,480.0/max(w,h))
    width=max(2,int(w*scale)//2*2);height=max(2,int(h*scale)//2*2)
    out=cv2.VideoWriter(target,cv2.VideoWriter_fourcc(*'mp4v'),min(fps,15.0),(width,height))
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
            cv2.rectangle(frame,(0,0),(width,65),(15,15,15),-1)
            cv2.putText(frame,'FINAL: '+str(decision.get('count',0))+'  '+str(decision.get('status','')).upper(),(10,25),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),2,cv2.LINE_AA)
            cv2.putText(frame,'Events not localized - final total only',(10,49),cv2.FONT_HERSHEY_SIMPLEX,.38,(190,190,190),1,cv2.LINE_AA)
            out.write(frame);written+=1;frame_index+=1
    finally:
        out.release();cap.release()
    if written==0:raise RuntimeError('No frames rendered')
    print(json.dumps({'frames':written,'size':[width,height]}))
if __name__=='__main__':main()
