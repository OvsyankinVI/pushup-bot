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
    ('libglvnd0_1.6.0-1_amd64.deb',
     'b6da5b153dd62d8b5e5fbe25242db1fc05c068707c365db49abda8c2427c75f8'),
    ('libgles2_1.6.0-1_amd64.deb',
     '07b2f51b8aa3c8d6d928133cd46087bd8793d0d67c203f09fc289d45a2cf5f47'),
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
                suffix = name[len('data.tar'):]
                with tempfile.NamedTemporaryFile(suffix=suffix) as data_file:
                    data_file.write(payload)
                    data_file.flush()
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
                try:
                    os.remove(deb_path)
                except OSError:
                    pass
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
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return MODEL_PATH


def _distance(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def _angle(a, b, c):
    ab = (a.x - b.x, a.y - b.y)
    cb = (c.x - b.x, c.y - b.y)
    denom = math.hypot(*ab) * math.hypot(*cb)
    if denom <= 1e-8:
        return None
    cosine = max(-1.0, min(1.0, (ab[0] * cb[0] + ab[1] * cb[1]) / denom))
    return math.degrees(math.acos(cosine))


def _median_smooth(values, radius=2):
    result = []
    for i in range(len(values)):
        window = [v for v in values[max(0, i-radius):i+radius+1] if v is not None]
        result.append(statistics.median(window) if window else None)
    return result


def _count_cycles(signal, sample_fps):
    valid = [v for v in signal if v is not None]
    if len(valid) < max(8, int(sample_fps * 2)):
        return 0, 0.0
    lo = statistics.quantiles(valid, n=10)[0]
    hi = statistics.quantiles(valid, n=10)[-1]
    amplitude = hi - lo
    if amplitude < 0.08:
        return 0, amplitude
    low_threshold = lo + amplitude * 0.35
    high_threshold = lo + amplitude * 0.65
    state = None
    count = 0
    last_transition = -999
    min_gap = max(1, int(sample_fps * 0.30))
    for i, value in enumerate(signal):
        if value is None:
            continue
        if state is None:
            if value >= high_threshold:
                state = 'high'
            elif value <= low_threshold:
                state = 'low'
            continue
        if state == 'high' and value <= low_threshold and i - last_transition >= min_gap:
            state = 'low'
            last_transition = i
        elif state == 'low' and value >= high_threshold and i - last_transition >= min_gap:
            count += 1
            state = 'high'
            last_transition = i
    return count, amplitude


def analyze_pose_visibility(video_path: str, sample_fps: float = 6.0) -> dict:
    _ensure_mediapipe_runtime()
    model_path = _ensure_model()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError('Video cannot be opened by OpenCV')
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps / sample_fps))
    actual_sample_fps = source_fps / every_n
    total_frames = sampled_frames = pose_frames = usable_frames = 0
    core_visibility_sum = supporting_visibility_sum = 0.0
    motion_signal = []
    elbow_angles = []

    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=model_path),
        running_mode=mp.tasks.vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=0.45, min_pose_presence_confidence=0.45,
        min_tracking_confidence=0.45)
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frame_index = total_frames
                total_frames += 1
                if frame_index % every_n:
                    continue
                sampled_frames += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    int(frame_index * 1000 / source_fps))
                if not result.pose_landmarks:
                    motion_signal.append(None)
                    elbow_angles.append(None)
                    continue
                pose_frames += 1
                lm = result.pose_landmarks[0]
                core = [lm[i].visibility for i in CORE_REQUIRED]
                supporting = [lm[i].visibility for i in SUPPORTING]
                core_mean = sum(core) / len(core)
                core_visibility_sum += core_mean
                supporting_visibility_sum += sum(supporting) / len(supporting)
                usable = min(core) >= 0.30 and core_mean >= 0.55
                if usable:
                    usable_frames += 1
                    shoulder_y = (lm[11].y + lm[12].y) / 2
                    hip_y = (lm[23].y + lm[24].y) / 2
                    torso = (_distance(lm[11], lm[23]) + _distance(lm[12], lm[24])) / 2
                    # Camera-scale invariant vertical shoulder/hip motion. Works without wrists.
                    motion_signal.append((shoulder_y + hip_y) / max(torso, 0.05))
                    angles = []
                    for s, e, w in ((11, 13, 15), (12, 14, 16)):
                        if min(lm[s].visibility, lm[e].visibility, lm[w].visibility) >= 0.35:
                            angle = _angle(lm[s], lm[e], lm[w])
                            if angle is not None:
                                angles.append(angle)
                    elbow_angles.append(sum(angles) / len(angles) if angles else None)
                else:
                    motion_signal.append(None)
                    elbow_angles.append(None)
    finally:
        capture.release()

    if sampled_frames == 0:
        raise ValueError('Video contains no readable sampled frames')
    smoothed_motion = _median_smooth(motion_signal)
    pushup_count, motion_amplitude = _count_cycles(smoothed_motion, actual_sample_fps)
    valid_angles = [a for a in elbow_angles if a is not None]
    angle_range = ((max(valid_angles) - min(valid_angles)) if len(valid_angles) >= 5 else 0.0)
    return {
        'total_frames': total_frames, 'sampled_frames': sampled_frames,
        'pose_frames': pose_frames, 'usable_frames': usable_frames,
        'pose_ratio': pose_frames / sampled_frames,
        'usable_ratio': usable_frames / sampled_frames,
        'mean_visibility': core_visibility_sum / pose_frames if pose_frames else 0.0,
        'supporting_visibility': supporting_visibility_sum / pose_frames if pose_frames else 0.0,
        'pushup_count': pushup_count, 'motion_amplitude': motion_amplitude,
        'elbow_angle_range': angle_range, 'actual_sample_fps': actual_sample_fps,
    }
