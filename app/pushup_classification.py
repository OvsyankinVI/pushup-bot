def classify_pushup_attempt(metrics: dict) -> dict:
    """Staging classifier using motion plus camera-invariant pose evidence.

    2D screen orientation is diagnostic only: a front-view push-up projects the
    shoulder/hip axis vertically and must never be rejected as a standing pose.
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
    angle_cycles = [c for c in arm if c.get('name') in ('left_angle', 'right_angle')
                    and int(c.get('count') or 0) > 0 and float(c.get('quality') or 0) >= 0.30]
    selected_metric = by_name.get(selected, {})
    selected_quality = float(selected_metric.get('quality') or 0.0)
    straight = float(geometry.get('straight_body_ratio') or 0.0)

    if pose < 0.35 or usable < 0.25:
        return {'status': 'uncertain', 'reason': 'insufficient_pose_visibility'}

    arm_evidence = (count >= 1 and selected not in ('body_y', 'none') and
                    selected_quality >= 0.30 and len(moving_arm) >= 1)

    # Until 3D calibration is complete, never use screen-horizontal/screen-vertical
    # ratios as a hard gate. Real front-view push-ups look vertical in image x/y.
    # Strong elbow flexion cycles + a mostly straight shoulder-hip-leg chain are
    # enough to keep a real attempt accepted for staging.
    if arm_evidence and angle_cycles and straight >= 0.15:
        return {'status': 'accepted', 'reason': 'arm_cycles_with_straight_body_chain'}

    if count == 0 and usable >= 0.65 and pose >= 0.75 and not moving_arm:
        return {'status': 'rejected', 'reason': 'no_pushup_cycles_detected'}

    if count > 0 and selected == 'body_y':
        return {'status': 'uncertain', 'reason': 'body_motion_without_arm_confirmation'}
    if agreement < 0.15 and count > 0:
        return {'status': 'uncertain', 'reason': 'weak_signal_agreement'}
    return {'status': 'uncertain', 'reason': 'needs_3d_pose_confirmation'}
