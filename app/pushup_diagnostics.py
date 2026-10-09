"""Compact staging-only push-up diagnostics; no effect on classification."""


def format_diagnostics(attempt_id, message_id, metrics, classification, elapsed):
    world = metrics.get('world_geometry') or {}
    geom = metrics.get('geometry') or {}
    front = metrics.get('front_cycles') or {}
    angle = metrics.get('angle_shadow') or {}
    full = metrics.get('full_cycle_shadow') or {}
    single = metrics.get('single_arm_shadow') or {}
    motion = metrics.get('motion_consistency') or {}
    reliability = metrics.get('signal_reliability') or {}

    def fmt(value, digits=2):
        return 'n/a' if value is None else f'{value:.{digits}f}'

    lines = [
        f"🏋️ ОТЖИМАНИЙ: {classification['count']} | {classification['status']}",
        f"Причина: {classification['reason']}",
        f"🔬 TEST | Attempt {attempt_id} | Message {message_id} | {elapsed:.1f}s",
        f"Кадры: {metrics['sampled_frames']} | Pose: {fmt(metrics['pose_ratio'])} | Usable: {fmt(metrics['usable_ratio'])}",
        f"2D: {metrics['pushup_count']} | 3D temporal: {world.get('gated_pushup_count', 0)}",
        f"Геометрия: 2D horizontal={fmt(geom.get('horizontal_ratio'))} | 3D horizontal={fmt(world.get('horizontal_ratio'))}",
        "— СЧЁТЧИКИ (LEFT / RIGHT) —",
        f"Front 2D: {(front.get('left') or {}).get('count', 0)} / {(front.get('right') or {}).get('count', 0)} | paired={(front.get('paired') or {}).get('count', 0)}",
        f"Angle: {(angle.get('left') or {}).get('count', 0)} / {(angle.get('right') or {}).get('count', 0)}",
        f"Full cycle: {(full.get('left') or {}).get('count', 0)} / {(full.get('right') or {}).get('count', 0)}",
        f"Single arm shadow: {single.get('suggested_count', 0)} ({single.get('suggested_side', 'none')})",
        "— НАДЁЖНОСТЬ СИГНАЛОВ —",
    ]
    for side in ('left', 'right'):
        info = reliability.get(side) or {}
        comp = info.get('components') or {}
        lines.extend([
            f"{side}: score={fmt(info.get('score'), 3)} | coverage={fmt(comp.get('coverage'), 3)} | visibility={fmt(comp.get('visibility'), 3)}",
            f"  regularity={fmt(comp.get('interval_regularity'), 3)} | amplitude={fmt(comp.get('amplitude_stability'), 3)} | interval={fmt(info.get('median_interval'))}s",
            f"  angle_agree={fmt(info.get('angle_agreement'), 3)} | full_agree={fmt(info.get('full_cycle_agreement'), 3)}",
        ])
    lines.append("— REPETITION VALIDATION SHADOW —")
    for side in ('left', 'right'):
        rv=(metrics.get('repetition_validation') or {}).get(side) or {}
        missing=rv.get('missing_evidence') or {}
        lines.append(f"{side}: candidates={rv.get('candidates',0)} confirmed={rv.get('confirmed',0)} uncertain={rv.get('uncertain',0)} rejected={rv.get('rejected',0)}")
        lines.append(f"  missing: angle={missing.get('no_angle',0)} shoulder={missing.get('weak_shoulder',0)} timing={missing.get('irregular_timing',0)} amplitude={missing.get('weak_amplitude',0)}")
    lines.append("— СОГЛАСОВАННОСТЬ ДВИЖЕНИЙ —")
    for side in ('left', 'right'):
        info = motion.get(side) or {}
        lines.append(
            f"{side}: phase={fmt(info.get('phase_agreement_ratio'))} | shoulder={fmt(info.get('median_shoulder_excursion'), 3)} | cycles={info.get('evaluated', 0)}"
        )
    lines.append("— 3D СЕГМЕНТЫ —")
    for seg in world.get('segment_details', []):
        if not seg.get('counted'):
            continue
        lines.append(
            f"{seg.get('start_s')}-{seg.get('end_s')}s | count={seg.get('count')} | elbow={fmt(seg.get('elbow_range'))}° | coupling={fmt(seg.get('motion_coupling'))}"
        )
    lines.append("Все shadow-показатели диагностические, на результат не влияют.")
    return ['\n'.join(lines)]
