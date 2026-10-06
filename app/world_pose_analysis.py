import math
import statistics

import cv2
import mediapipe as mp

from app.pose_analysis import _ensure_mediapipe_runtime, _ensure_model


def _midpoint(a, b):
    return ((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2)


def _vector(a, b):
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _angle3(a, b, c):
    ba = (a.x - b.x, a.y - b.y, a.z - b.z)
    bc = (c.x - b.x, c.y - b.y, c.z - b.z)
    denom = _norm(ba) * _norm(bc)
    if denom <= 1e-8:
        return None
    cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(ba, bc)) / denom))
    return math.degrees(math.acos(cosine))


def analyze_world_pose(video_path: str, sample_fps: float = 3.0) -> dict:
    """Collect camera-orientation-resistant MediaPipe world-landmark diagnostics.

    This is diagnostic-only for staging. It intentionally does not influence
    classification yet. A separate low-FPS pass keeps the existing counter
    untouched while we validate whether 3D geometry separates push-ups from
    standing/squats/plank.
    """
    _ensure_mediapipe_runtime()
    model = _ensure_model()
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError('Video cannot be opened for world pose analysis')

    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps / sample_fps))
    sampled = world_frames = horizontal = vertical = straight = pushup_like = 0
    torso_verticalities = []
    body_angles = []

    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=model),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=.45,
        min_pose_presence_confidence=.45,
        min_tracking_confidence=.45,
    )
    frame_index = 0
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                idx = frame_index
                frame_index += 1
                if idx % every_n:
                    continue
                sampled += 1
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                    int(idx * 1000 / source_fps),
                )
                if not result.pose_world_landmarks:
                    continue
                world_frames += 1
                lm = result.pose_world_landmarks[0]
                shoulder = _midpoint(lm[11], lm[12])
                hip = _midpoint(lm[23], lm[24])
                torso = _vector(shoulder, hip)
                torso_len = _norm(torso)
                if torso_len <= 1e-8:
                    continue

                # MediaPipe world Y is vertical. This ratio is independent of
                # whether the camera views the person from front or side.
                verticality = abs(torso[1]) / torso_len
                torso_verticalities.append(verticality)
                if verticality <= .55:
                    horizontal += 1
                if verticality >= .75:
                    vertical += 1

                side_angles = []
                for shoulder_i, hip_i, ankle_i in ((11, 23, 27), (12, 24, 28)):
                    angle = _angle3(lm[shoulder_i], lm[hip_i], lm[ankle_i])
                    if angle is not None:
                        side_angles.append(angle)
                if side_angles:
                    body_angle = max(side_angles)
                    body_angles.append(body_angle)
                    if body_angle >= 145:
                        straight += 1
                    if verticality <= .60 and body_angle >= 140:
                        pushup_like += 1
    finally:
        capture.release()

    denominator = world_frames or 1
    return {
        'sampled_frames': sampled,
        'world_frames': world_frames,
        'world_pose_ratio': world_frames / sampled if sampled else 0.0,
        'horizontal_ratio': horizontal / denominator,
        'vertical_ratio': vertical / denominator,
        'straight_body_ratio': straight / denominator,
        'pushup_pose_ratio': pushup_like / denominator,
        'median_torso_verticality': statistics.median(torso_verticalities) if torso_verticalities else None,
        'median_body_line_deg': statistics.median(body_angles) if body_angles else None,
    }
