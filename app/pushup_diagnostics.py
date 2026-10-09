"""Read-only staging diagnostic formatting. No database schema changes."""
def format_diagnostics(attempt_id, message_id, metrics, classification, elapsed):
    world=metrics.get('world_geometry') or {}
    geom=metrics.get('geometry') or {}
    def num(value, digits=2):
        return 'n/a' if value is None else f'{value:.{digits}f}'
    lines=[
        f"🏋️ ОПРЕДЕЛЕНО ОТЖИМАНИЙ: {classification['count']}",
        f"Результат: {classification['status']} | {classification['reason']}",
        '🔬 PUSHUP DIAGNOSTICS',
        f"Attempt: {attempt_id} | Message: {message_id}",
        f"Status: {classification['status']} | Count: {classification['count']}",
        f"Reason: {classification['reason']}",
        f"Processing: {elapsed:.1f}s | Sample FPS: {num(metrics.get('actual_sample_fps'))}",
        f"Frames: {metrics['sampled_frames']}/{metrics['total_frames']} | Pose: {metrics['pose_frames']} | Usable: {metrics['usable_frames']}",
        f"Pose ratio: {num(metrics['pose_ratio'])} | Usable ratio: {num(metrics['usable_ratio'])}",
        f"Visibility: {num(metrics['mean_visibility'])} | Supporting: {num(metrics['supporting_visibility'])}",
        f"Dominant: {metrics['dominant_side']} | Signal: {metrics['selected_signal']}",
        f"2D count: {metrics['pushup_count']} | Agreement: {num(metrics['signal_agreement'])}",
        f"2D horizontal: {num(geom.get('horizontal_ratio'))} | Straight: {num(geom.get('straight_body_ratio'))}",
        f"3D coverage: {num(world.get('world_pose_ratio'))} | Horizontal: {num(world.get('horizontal_ratio'))} | Vertical: {num(world.get('vertical_ratio'))}",
        f"3D pushup pose: {num(world.get('pushup_pose_ratio'))} | Temporal count: {world.get('gated_pushup_count',0)}",
        f"3D segments: {world.get('gated_segments',0)} | Counted: {world.get('counted_segments',0)} | Gated ratio: {num(world.get('gated_ratio'))}",
        '— 2D SIGNALS —',
    ]
    for item in metrics.get('candidates',[]):
        lines.append(f"{item['name']}: cycles={item['count']}, amp={item['amplitude']}, quality={item['quality']}")
    lines.append('— FRONT VIEW 2D CYCLES —')
    for side,info in (metrics.get('front_cycles') or {}).items():
        if side=='paired':
            lines.append(f"paired cycles={info['count']}")
            for pair in info['details']:
                l=pair['left'];r=pair['right']
                lines.append(f"{pair['time']}s L[{l['start']}-{l['end']}] rise/fall={l['rise']}/{l['fall']} R[{r['start']}-{r['end']}] rise/fall={r['rise']}/{r['fall']}")
        else:
            lines.append(f"{side}: cycles={info['count']} amp={info['amplitude']} timestamps={info['timestamps']}")
    shadow=metrics.get('single_arm_shadow') or {}
    lines.append('— SINGLE ARM SHADOW (DOES NOT AFFECT COUNT) —')
    lines.append(f"Suggested: {shadow.get('suggested_count',0)} | side: {shadow.get('suggested_side','none')} | mode: diagnostic_only")
    for item in shadow.get('candidates',[]):
        lines.append(f"{item['side']}: cycles={item['count']} eligible={item['eligible']} coverage={item['coverage']} visibility={item['visibility']} median_rise_fall={item['median_strength']} angle_cycles={item['angle_cycles']} angle_amp={item['angle_amplitude']}")
    lines.append('— MOTION CONSISTENCY SHADOW (NO COUNT EFFECT) —')
    for side,info in (metrics.get('motion_consistency') or {}).items():
        lines.append(f"{side}: evaluated={info['evaluated']} phase_agreement={num(info['phase_agreement_ratio'])} median_shoulder_excursion={num(info['median_shoulder_excursion'],3)}")
        for cycle in info.get('cycles',[]):
            lines.append(f"{side} {cycle['time']}s phase={cycle['phase_agree']} shoulder={cycle['shoulder_excursion']} elbow={cycle['elbow_excursion']} relative={cycle['relative_shoulder_motion']}")
    lines.append('— 3D SEGMENTS —')
    for i,s in enumerate(world.get('segment_details',[]),1):
        lines.extend([
            f"#{i} {s['start_s']}-{s['end_s']}s frames={s['frames']} cycles={s['count']} counted={s['counted']}",
            f"elbow_range={num(s.get('elbow_range'))}° verticality={num(s.get('median_verticality'))} p75={num(s.get('p75_verticality'))} p90={num(s.get('p90_verticality'))}",
            f"horizontal={num(s.get('strict_horizontal_ratio'))} coupling={num(s.get('motion_coupling'))} ({s.get('coupling_samples')})",
            f"pre/post verticality={num(s.get('pre_verticality'))}/{num(s.get('post_verticality'))}",
            f"hip_range={num(s.get('hip_y_range'))} knee_range={num(s.get('knee_range_deg'))} knee_sync={num(s.get('knee_elbow_sync'))}",
            f"left/right leg visibility={num(s.get('left_leg_visibility'))}/{num(s.get('right_leg_visibility'))} reliable={s.get('reliable_leg_sides')}",
        ])
    if not world.get('segment_details'):lines.append('No 3D segments passed segmentation minimum duration.')
    lines.append('Classifier: 3D temporal primary; dual-arm 2D fallback when 3D horizontal and 3D elbow cycles absent.')
    # Telegram hard limit is 4096 characters; split only at line boundaries.
    chunks=[];current=''
    for line in lines:
        if len(current)+len(line)+1>3500:
            chunks.append(current);current=''
        current+=line+'\n'
    if current:chunks.append(current)
    return chunks
