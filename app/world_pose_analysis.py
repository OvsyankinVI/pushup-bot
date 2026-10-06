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


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _segments(mask, min_frames=3, bridge_gap=1):
    """Return stable True runs, bridging a single missed pose frame."""
    mask = list(mask)
    for i in range(1, len(mask) - 1):
        if not mask[i] and mask[i - 1] and mask[i + 1] and bridge_gap >= 1:
            mask[i] = True
    result = []
    start = None
    for i, value in enumerate(mask + [False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            if i - start >= min_frames:
                result.append((start, i))
            start = None
    return result


def _count_elbow_cycles(values, min_range=35.0):
    """Count full flexion/extension cycles inside an already gated push-up segment."""
    valid = [v for v in values if v is not None]
    if len(valid) < 5:
        return 0, 0.0
    ordered = sorted(valid)
    lo = ordered[int((len(ordered) - 1) * .15)]
    hi = ordered[int((len(ordered) - 1) * .85)]
    amplitude = hi - lo
    if amplitude < min_range:
        return 0, amplitude
    low = lo + amplitude * .30
    high = lo + amplitude * .70
    state = None
    transitions = 0
    for value in values:
        if value is None:
            continue
        if state is None:
            if value <= low:
                state = 'bent'
            elif value >= high:
                state = 'straight'
        elif state == 'straight' and value <= low:
            state = 'bent'; transitions += 1
        elif state == 'bent' and value >= high:
            state = 'straight'; transitions += 1
    return transitions // 2, amplitude


def analyze_world_pose(video_path: str, sample_fps: float = 6.0) -> dict:
    """3D diagnostics plus temporal push-up gating for staging calibration."""
    _ensure_mediapipe_runtime(); model = _ensure_model(); capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise ValueError('Video cannot be opened for world pose analysis')
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    every_n = max(1, round(source_fps / sample_fps)); actual_fps = source_fps / every_n
    sampled = world_frames = horizontal = vertical = straight = pushup_like = 0
    torso_verticalities = []; body_angles = []; timeline = []
    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=model), running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=1, min_pose_detection_confidence=.45, min_pose_presence_confidence=.45, min_tracking_confidence=.45)
    frame_index = 0
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                ok, frame = capture.read()
                if not ok: break
                idx = frame_index; frame_index += 1
                if idx % every_n: continue
                sampled += 1
                result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB,
                    data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), int(idx * 1000 / source_fps))
                if not result.pose_world_landmarks:
                    timeline.append(None); continue
                world_frames += 1; lm = result.pose_world_landmarks[0]
                shoulder = _midpoint(lm[11], lm[12]); hip = _midpoint(lm[23], lm[24]); torso = _vector(shoulder, hip); torso_len = _norm(torso)
                if torso_len <= 1e-8:
                    timeline.append(None); continue
                verticality = abs(torso[1]) / torso_len; torso_verticalities.append(verticality)
                if verticality <= .55: horizontal += 1
                if verticality >= .75: vertical += 1
                side_angles = []; elbow_angles = []
                for shoulder_i, hip_i, ankle_i, elbow_i, wrist_i in ((11,23,27,13,15),(12,24,28,14,16)):
                    body_angle = _angle3(lm[shoulder_i], lm[hip_i], lm[ankle_i])
                    if body_angle is not None: side_angles.append(body_angle)
                    elbow_angle = _angle3(lm[shoulder_i], lm[elbow_i], lm[wrist_i])
                    if elbow_angle is not None: elbow_angles.append(elbow_angle)
                body_angle = max(side_angles) if side_angles else None; elbow_angle = _median(elbow_angles)
                if body_angle is not None:
                    body_angles.append(body_angle)
                    if body_angle >= 145: straight += 1
                    if verticality <= .60 and body_angle >= 140: pushup_like += 1
                # Relaxed gate: includes front and side push-ups, excludes clearly upright exercise.
                in_pushup_pose = body_angle is not None and body_angle >= 135 and verticality <= .68
                timeline.append({'pose': in_pushup_pose, 'elbow': elbow_angle, 'verticality': verticality, 'body_angle': body_angle})
    finally:
        capture.release()

    pose_mask = [bool(row and row['pose']) for row in timeline]
    min_segment_frames = max(3, int(actual_fps * 1.0))
    segments = _segments(pose_mask, min_frames=min_segment_frames)
    segment_details = []; gated_count = 0; gated_frames = 0
    for start, end in segments:
        elbows = [timeline[i]['elbow'] if timeline[i] else None for i in range(start, end)]
        count, amplitude = _count_elbow_cycles(elbows)
        gated_count += count; gated_frames += end - start
        segment_details.append({'start_s': round(start / actual_fps, 2), 'end_s': round(end / actual_fps, 2),
                                'frames': end - start, 'count': count, 'elbow_range': round(amplitude, 1)})
    denominator = world_frames or 1
    return {
        'sampled_frames': sampled, 'world_frames': world_frames,
        'world_pose_ratio': world_frames / sampled if sampled else 0.0,
        'horizontal_ratio': horizontal / denominator, 'vertical_ratio': vertical / denominator,
        'straight_body_ratio': straight / denominator, 'pushup_pose_ratio': pushup_like / denominator,
        'median_torso_verticality': _median(torso_verticalities), 'median_body_line_deg': _median(body_angles),
        'gated_frames': gated_frames, 'gated_ratio': gated_frames / sampled if sampled else 0.0,
        'gated_segments': len(segments), 'gated_pushup_count': gated_count,
        'segment_details': segment_details, 'actual_sample_fps': actual_fps,
    }
