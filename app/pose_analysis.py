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
CORE_REQUIRED = (11, 12, 13, 14, 23, 24)
SUPPORTING = (15, 16, 25, 26, 27, 28)
_runtime_loaded = False


def _download_checked(url, path, sha256):
    urllib.request.urlretrieve(url, path)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise RuntimeError('Downloaded runtime package checksum mismatch')


def _extract_deb_data(deb_path, destination):
    with open(deb_path, 'rb') as handle:
        if handle.read(8) != b'!<arch>\n':
            raise RuntimeError('Invalid Debian package archive')
        while True:
            header = handle.read(60)
            if not header:
                break
            name = header[:16].decode('ascii').strip().rstrip('/')
            size = int(header[48:58].decode('ascii').strip())
            payload = handle.read(size)
            if size % 2:
                handle.read(1)
            if name.startswith('data.tar'):
                with tempfile.NamedTemporaryFile(suffix=name[len('data.tar'):]) as data_file:
                    data_file.write(payload); data_file.flush()
                    with tarfile.open(data_file.name, mode='r:*') as archive:
                        archive.extractall(destination, filter='data')
                return
    raise RuntimeError('Debian package has no data archive')


def _ensure_mediapipe_runtime():
    global _runtime_loaded
    if _runtime_loaded:
        return
    lib_dir = os.path.join(RUNTIME_DIR, 'usr', 'lib', 'x86_64-linux-gnu')
    gles = os.path.join(lib_dir, 'libGLESv2.so.2')
    dispatch = os.path.join(lib_dir, 'libGLdispatch.so.0')
    if not os.path.isfile(gles) or not os.path.isfile(dispatch):
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        for filename, checksum in DEBS:
            deb_path = os.path.join('/tmp', filename)
            _download_checked(DEBIAN_POOL + filename, deb_path, checksum)
            try:
                _extract_deb_data(deb_path, RUNTIME_DIR)
            finally:
                try: os.remove(deb_path)
                except OSError: pass
    ctypes.CDLL(dispatch, mode=ctypes.RTLD_GLOBAL)
    ctypes.CDLL(gles, mode=ctypes.RTLD_GLOBAL)
    _runtime_loaded = True


def _ensure_model():
    if os.path.isfile(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0:
        return MODEL_PATH
    tmp_path = MODEL_PATH + '.download'
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp_path)
        os.replace(tmp_path, MODEL_PATH)
    finally:
        if os.path.exists(tmp_path): os.remove(tmp_path)
    return MODEL_PATH


def _distance(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def _angle(a, b, c):
    ab = (a.x-b.x, a.y-b.y); cb = (c.x-b.x, c.y-b.y)
    denom = math.hypot(*ab) * math.hypot(*cb)
    if denom <= 1e-8: return None
    cosine = max(-1.0, min(1.0, (ab[0]*cb[0]+ab[1]*cb[1])/denom))
    return math.degrees(math.acos(cosine))


def _smooth(values, radius=2):
    result = []
    for i in range(len(values)):
        window = [v for v in values[max(0,i-radius):i+radius+1] if v is not None]
        result.append(statistics.median(window) if window else None)
    return result


def _count_cycles(values, fps, min_amplitude):
    valid = [v for v in values if v is not None]
    if len(valid) < max(8, int(fps*2)):
        return 0, 0.0, 0.0
    ordered = sorted(valid)
    lo = ordered[int((len(ordered)-1)*0.12)]
    hi = ordered[int((len(ordered)-1)*0.88)]
    amplitude = hi-lo
    if amplitude < min_amplitude:
        return 0, amplitude, 0.0
    low = lo + amplitude*0.32; high = lo + amplitude*0.68
    state = None; count = 0; transitions = 0; last = -999
    min_gap = max(1, int(fps*0.22))
    for i, value in enumerate(values):
        if value is None: continue
        if state is None:
            if value >= high: state = 'high'
            elif value <= low: state = 'low'
        elif state == 'high' and value <= low and i-last >= min_gap:
            state = 'low'; transitions += 1; last = i
        elif state == 'low' and value >= high and i-last >= min_gap:
            state = 'high'; transitions += 1; count += 1; last = i
    coverage = len(valid)/len(values)
    quality = coverage * min(1.0, transitions/max(2, count*2)) if count else 0.0
    return count, amplitude, quality


def _choose_consensus(candidates):
    active = [c for c in candidates if c['count'] > 0 and c['quality'] >= 0.30]
    if not active:
        return 0, 'none', 0.0
    active.sort(key=lambda c: c['count'])
    total = sum(c['quality'] for c in active); running = 0.0; chosen = active[-1]
    for candidate in active:
        running += candidate['quality']
        if running >= total/2:
            chosen = candidate; break
    agreement = sum(c['quality'] for c in active if abs(c['count']-chosen['count']) <= 1)/total
    return chosen['count'], chosen['name'], agreement


def analyze_pose_visibility(video_path: str, sample_fps: float = 6.0) -> dict:
    _ensure_mediapipe_runtime(); model_path = _ensure_model()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened(): raise ValueError('Video cannot be opened by OpenCV')
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps/sample_fps)); actual_fps = source_fps/every_n
    total = sampled = pose_frames = usable = 0; core_sum = supporting_sum = 0.0
    signals = {name: [] for name in ('body_y','left_elbow_y','right_elbow_y','left_angle','right_angle')}
    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=model_path),
        running_mode=mp.tasks.vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=.45, min_pose_presence_confidence=.45,
        min_tracking_confidence=.45)
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok: break
                frame_index = total; total += 1
                if frame_index % every_n: continue
                sampled += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    int(frame_index*1000/source_fps))
                if not result.pose_landmarks:
                    for values in signals.values(): values.append(None)
                    continue
                pose_frames += 1; lm = result.pose_landmarks[0]
                core = [lm[i].visibility for i in CORE_REQUIRED]
                supporting = [lm[i].visibility for i in SUPPORTING]
                core_mean = sum(core)/len(core); core_sum += core_mean
                supporting_sum += sum(supporting)/len(supporting)
                if min(core) < .30 or core_mean < .55:
                    for values in signals.values(): values.append(None)
                    continue
                usable += 1
                torso = max((_distance(lm[11],lm[23])+_distance(lm[12],lm[24]))/2, .05)
                shoulder_y = (lm[11].y+lm[12].y)/2; hip_y = (lm[23].y+lm[24].y)/2
                signals['body_y'].append((shoulder_y+hip_y)/(2*torso))
                signals['left_elbow_y'].append((lm[13].y-lm[11].y)/torso)
                signals['right_elbow_y'].append((lm[14].y-lm[12].y)/torso)
                for name,s,e,w in (('left_angle',11,13,15),('right_angle',12,14,16)):
                    if min(lm[s].visibility,lm[e].visibility,lm[w].visibility) >= .35:
                        signals[name].append(_angle(lm[s],lm[e],lm[w]))
                    else:
                        signals[name].append(None)
    finally:
        capture.release()
    if sampled == 0: raise ValueError('Video contains no readable sampled frames')

    candidates = []
    thresholds = {'body_y':.05,'left_elbow_y':.04,'right_elbow_y':.04,'left_angle':18.0,'right_angle':18.0}
    for name, values in signals.items():
        count, amplitude, quality = _count_cycles(_smooth(values), actual_fps, thresholds[name])
        candidates.append({'name':name,'count':count,'amplitude':round(amplitude,3),'quality':round(quality,3)})
    pushup_count, selected_signal, agreement = _choose_consensus(candidates)
    return {
        'total_frames':total, 'sampled_frames':sampled, 'pose_frames':pose_frames,
        'usable_frames':usable, 'pose_ratio':pose_frames/sampled,
        'usable_ratio':usable/sampled,
        'mean_visibility':core_sum/pose_frames if pose_frames else 0.0,
        'supporting_visibility':supporting_sum/pose_frames if pose_frames else 0.0,
        'pushup_count':pushup_count, 'selected_signal':selected_signal,
        'signal_agreement':agreement, 'candidates':candidates,
        'motion_amplitude':next(c['amplitude'] for c in candidates if c['name']=='body_y'),
        'elbow_angle_range':max((c['amplitude'] for c in candidates if 'angle' in c['name']), default=0.0),
        'actual_sample_fps':actual_fps,
    }
