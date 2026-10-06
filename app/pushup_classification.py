def classify_pushup_attempt(metrics: dict) -> dict:
    """Staging classifier v2: temporal 3D evidence is primary.

    Screen-space orientation/bodyline and leg visibility are diagnostic only.
    """
    usable=float(metrics.get('usable_ratio') or 0.0)
    pose=float(metrics.get('pose_ratio') or 0.0)
    legacy_count=int(metrics.get('pushup_count') or 0)
    selected=metrics.get('selected_signal','none')
    agreement=float(metrics.get('signal_agreement') or 0.0)
    candidates=metrics.get('candidates') or []
    world=metrics.get('world_geometry') or {}
    temporal_count=int(world.get('gated_pushup_count') or 0)
    segments=[s for s in (world.get('segment_details') or []) if s.get('counted',True)]
    horizontal=float(world.get('horizontal_ratio') or 0.0)
    vertical=float(world.get('vertical_ratio') or 0.0)
    pushup_pose=float(world.get('pushup_pose_ratio') or 0.0)

    if pose < 0.35 or usable < 0.25:
        return {'status':'uncertain','reason':'insufficient_pose_visibility','count':0}

    # Squat-like false positive observed in calibration: only a short horizontal
    # interval inside an otherwise vertical video. Do not require leg landmarks:
    # their visibility may be poor even in this case.
    if temporal_count > 0 and vertical >= 0.60 and horizontal <= 0.35 and pushup_pose <= 0.15:
        return {'status':'uncertain','reason':'vertical_motion_not_confirmed_as_pushups','count':0}

    # Long coherent 3D temporal cycles are our strongest camera-invariant evidence.
    if temporal_count > 0:
        final_count=temporal_count
        # Strong side-view consensus can recover a single boundary repetition
        # missed by temporal segmentation (calibration case 34 -> 33).
        strong=[int(x.get('count') or 0) for x in candidates
                if x.get('name') in ('left_elbow_y','right_elbow_y','left_angle','right_angle')
                and float(x.get('quality') or 0.0) >= 0.80]
        near=[n for n in strong if abs(n-temporal_count) <= 1]
        if agreement >= 0.80 and near:
            final_count=max([temporal_count]+near)
        return {'status':'accepted','reason':'temporal_3d_pushup_cycles','count':final_count}

    if legacy_count == 0 and usable >= 0.65 and pose >= 0.75:
        return {'status':'rejected','reason':'no_pushup_cycles_detected','count':0}

    if legacy_count > 0 and selected == 'body_y':
        return {'status':'uncertain','reason':'body_motion_without_arm_confirmation','count':0}
    return {'status':'uncertain','reason':'needs_3d_pose_confirmation','count':0}
