def classify_pushup_attempt(metrics: dict) -> dict:
    """Conservative v1 classifier for staging calibration.

    This intentionally prefers uncertain over a false rejection. It classifies
    pose/count metrics only; daily_reports remains independent during staging.
    """
    usable = float(metrics.get('usable_ratio') or 0.0)
    pose = float(metrics.get('pose_ratio') or 0.0)
    count = int(metrics.get('pushup_count') or 0)
    selected = metrics.get('selected_signal', 'none')
    agreement = float(metrics.get('signal_agreement') or 0.0)
    candidates = metrics.get('candidates') or []

    by_name = {c.get('name'): c for c in candidates}
    arm = [c for c in candidates if c.get('name') in (
        'left_elbow_y', 'right_elbow_y', 'left_angle', 'right_angle')]
    moving_arm = [c for c in arm if int(c.get('count') or 0) > 0 and float(c.get('quality') or 0) >= 0.20]
    selected_metric = by_name.get(selected, {})
    selected_quality = float(selected_metric.get('quality') or 0.0)

    if pose < 0.35 or usable < 0.25:
        return {'status': 'uncertain', 'reason': 'insufficient_pose_visibility'}

    # A valid push-up decision needs cyclic arm evidence. Body movement alone is
    # deliberately not enough (helps avoid squats / bending / camera movement).
    if count >= 1 and selected != 'body_y' and selected != 'none' and selected_quality >= 0.30:
        if len(moving_arm) >= 1:
            return {'status': 'accepted', 'reason': 'cyclic_arm_motion'}

    # Reject only when pose tracking is strong enough that absence of arm cycles
    # is meaningful. Borderline cases remain uncertain for calibration.
    if count == 0 and usable >= 0.65 and pose >= 0.75 and not moving_arm:
        return {'status': 'rejected', 'reason': 'no_pushup_cycles_detected'}

    if count > 0 and selected == 'body_y':
        return {'status': 'uncertain', 'reason': 'body_motion_without_arm_confirmation'}
    if agreement < 0.15 and count > 0:
        return {'status': 'uncertain', 'reason': 'weak_signal_agreement'}
    return {'status': 'uncertain', 'reason': 'insufficient_pushup_evidence'}
