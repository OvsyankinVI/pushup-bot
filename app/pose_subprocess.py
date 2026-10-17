"""One-video pose analysis in an isolated interpreter (staging experiment)."""
import json
import sys
from app.pose_analysis import analyze_pose_visibility
from app.world_pose_analysis import analyze_world_landmarks

def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m app.pose_subprocess VIDEO_PATH")
    result = analyze_pose_visibility(sys.argv[1], 6.0, analyze_world_landmarks)
    sys.stdout.write(json.dumps(result, allow_nan=False) + "\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
