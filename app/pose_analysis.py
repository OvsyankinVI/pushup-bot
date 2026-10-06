import os
import urllib.request

import cv2
import mediapipe as mp

MODEL_URL = ('https://storage.googleapis.com/mediapipe-models/pose_landmarker/'
             'pose_landmarker_lite/float16/latest/pose_landmarker_lite.task')
MODEL_PATH = '/tmp/pose_landmarker_lite.task'

# MediaPipe pose landmark indexes required for push-up geometry.
REQUIRED = (11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)


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
    model_path = _ensure_model()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError('Video cannot be opened by OpenCV')

    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps / sample_fps))
    total_frames = 0
    sampled_frames = 0
    pose_frames = 0
    usable_frames = 0
    visibility_sum = 0.0

    BaseOptions = mp.tasks.BaseOptions
    PoseLandmarker = mp.tasks.vision.PoseLandmarker
    PoseLandmarkerOptions = mp.tasks.vision.PoseLandmarkerOptions
    VisionRunningMode = mp.tasks.vision.RunningMode
    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=model_path),
        running_mode=VisionRunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    try:
        with PoseLandmarker.create_from_options(options) as landmarker:
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
                visibilities = [landmarks[i].visibility for i in REQUIRED]
                mean_visibility = sum(visibilities) / len(visibilities)
                visibility_sum += mean_visibility
                # All core body/arm landmarks should be at least moderately visible.
                if min(visibilities) >= 0.35 and mean_visibility >= 0.60:
                    usable_frames += 1
    finally:
        capture.release()

    if sampled_frames == 0:
        raise ValueError('Video contains no readable sampled frames')

    pose_ratio = pose_frames / sampled_frames
    usable_ratio = usable_frames / sampled_frames
    mean_visibility = visibility_sum / pose_frames if pose_frames else 0.0
    return {
        'total_frames': total_frames,
        'sampled_frames': sampled_frames,
        'pose_frames': pose_frames,
        'usable_frames': usable_frames,
        'pose_ratio': pose_ratio,
        'usable_ratio': usable_ratio,
        'mean_visibility': mean_visibility,
    }
