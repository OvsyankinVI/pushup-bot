def classify_pushup_attempt(metrics: dict) -> dict:
    """Staging classifier with pose-geometry evidence.

    Thresholds are intentionally conservative while we collect calibration data.
    daily_reports remains independent during staging.
    """
    usable = float(metrics.get('usable_ratio') or 0.0)
    pose = float(metrics.get('pose_ratio') or 0.0)
    count = int(metrics.get('pushup_count') or 0)
    selected = metrics.get('selected_signal', 'none')
    agreement = float(metrics.get('signal_agreement') or 0.0)
    candidates = metrics.get('candidates') or []
    geometry = metrics.get('geometry') or {}

    by_name = {c.get('name'): c for c in candidates}
    arm = [c for c in candidates if c.get('name') in (
        'left_elbow_y', 'right_elbow_y', 'left_angle', 'right_angle')]
    moving_arm = [c for c in arm if int(c.get('count') or 0) > 0 and float(c.get('quality') or 0) >= 0.20]
    selected_metric = by_name.get(selected, {})
    selected_quality = float(selected_metric.get('quality') or 0.0)

    horizontal = float(geometry.get('horizontal_ratio') or 0.0)
    vertical = float(geometry.get('vertical_ratio') or 0.0)
    straight = float(geometry.get('straight_body_ratio') or 0.0)
    pushup_pose = float(geometry.get('pushup_pose_ratio') or 0.0)
    geometry_frames = int(geometry.get('frames') or 0)

    if pose < 0.35 or usable < 0.25:
        return {'status': 'uncertain', 'reason': 'insufficient_pose_visibility'}

    # Clear standing/squat geometry should not be accepted merely because arms
    # move cyclically. We keep borderline geometry uncertain until calibrated.
    if geometry_frames >= 8 and vertical >= 0.65 and horizontal < 0.20:
        return {'status': 'rejected', 'reason': 'upright_body_geometry'}

    arm_evidence = (count >= 1 and selected not in ('body_y', 'none') and
                    selected_quality >= 0.30 and len(moving_arm) >= 1)

    # Accept only when cyclic arm motion is accompanied by meaningful time in a
    # push-up-like body orientation. Hands/wrists are deliberately not required.
    if arm_evidence and pushup_pose >= 0.25 and horizontal >= 0.30:
        return {'status': 'accepted', 'reason': 'pushup_geometry_and_arm_cycles'}

    if arm_evidence and (pushup_pose < 0.10 or horizontal < 0.15):
        return {'status': 'rejected', 'reason': 'arm_cycles_without_pushup_geometry'}

    if count == 0 and usable >= 0.65 and pose >= 0.75 and not moving_arm:
        return {'status': 'rejected', 'reason': 'no_pushup_cycles_detected'}

    if count > 0 and selected == 'body_y':
        return {'status': 'uncertain', 'reason': 'body_motion_without_arm_confirmation'}
    if agreement < 0.15 and count > 0:
        return {'status': 'uncertain', 'reason': 'weak_signal_agreement'}
    if straight < 0.10 and geometry_frames >= 8:
        return {'status': 'uncertain', 'reason': 'body_line_not_visible_or_not_straight'}
    return {'status': 'uncertain', 'reason': 'insufficient_pushup_geometry_evidence'}
