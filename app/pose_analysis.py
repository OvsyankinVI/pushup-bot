import ctypes
import hashlib
import os
import struct
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

# For imperfect Telegram circles, wrists and legs are deliberately NOT mandatory.
# Shoulders/elbows/hips are the core signal; wrists/legs will later improve confidence.
CORE_REQUIRED = (11, 12, 13, 14, 23, 24)
SUPPORTING = (15, 16, 25, 26, 27, 28)
_runtime_loaded = False


def _download_checked(url: str, path: str, sha256: str):
    urllib.request.urlretrieve(url, path)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != sha256:
        raise RuntimeError('Downloaded runtime package checksum mismatch')


def _extract_deb_data(deb_path: str, destination: str):
    # A .deb is an ar archive. Parse it directly so no root/apt/dpkg is required.
    with open(deb_path, 'rb') as handle:
        if handle.read(8) != b'!<arch>\n':
            raise RuntimeError('Invalid Debian package archive')
        while True:
            header = handle.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b'`\n':
                raise RuntimeError('Invalid Debian package member')
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
    # Preload with RTLD_GLOBAL so MediaPipe's native binding can resolve the SONAME.
    ctypes.CDLL(dispatch, mode=ctypes.RTLD_GLOBAL)
    ctypes.CDLL(gles, mode=ctypes.RTLD_GLOBAL)
    _runtime_loaded = True


def _ensure_model():
    if os.path.isfile(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0:
        return MODEL_PATH
    tmp_path = MODEL_PATH + '.download'
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp_path)
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) <= 0:
            raise RuntimeError('Pose model download produced an empty file')
        os.replace(tmp_path, MODEL_PATH)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    return MODEL_PATH


def analyze_pose_visibility(video_path: str, sample_fps: float = 3.0) -> dict:
    _ensure_mediapipe_runtime()
    model_path = _ensure_model()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError('Video cannot be opened by OpenCV')

    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps / sample_fps))
    total_frames = sampled_frames = pose_frames = usable_frames = 0
    core_visibility_sum = supporting_visibility_sum = 0.0

    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=model_path),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.45,
        min_pose_presence_confidence=0.45,
        min_tracking_confidence=0.45,
    )

    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frame_index = total_frames
                total_frames += 1
                if frame_index % every_n != 0:
                    continue
                sampled_frames += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                timestamp_ms = int(frame_index * 1000 / source_fps)
                result = landmarker.detect_for_video(image, timestamp_ms)
                if not result.pose_landmarks:
                    continue
                pose_frames += 1
                landmarks = result.pose_landmarks[0]
                core = [landmarks[i].visibility for i in CORE_REQUIRED]
                supporting = [landmarks[i].visibility for i in SUPPORTING]
                core_mean = sum(core) / len(core)
                supporting_mean = sum(supporting) / len(supporting)
                core_visibility_sum += core_mean
                supporting_visibility_sum += supporting_mean
                # Missing wrists/feet must not reject otherwise useful push-up footage.
                if min(core) >= 0.30 and core_mean >= 0.55:
                    usable_frames += 1
    finally:
        capture.release()

    if sampled_frames == 0:
        raise ValueError('Video contains no readable sampled frames')

    return {
        'total_frames': total_frames,
        'sampled_frames': sampled_frames,
        'pose_frames': pose_frames,
        'usable_frames': usable_frames,
        'pose_ratio': pose_frames / sampled_frames,
        'usable_ratio': usable_frames / sampled_frames,
        'mean_visibility': core_visibility_sum / pose_frames if pose_frames else 0.0,
        'supporting_visibility': supporting_visibility_sum / pose_frames if pose_frames else 0.0,
    }
