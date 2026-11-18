"""Run local synthetic regression checks from an isolated working directory."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vision",
        action="store_true",
        help="also run vision tests using the installed compatible AstrBot",
    )
    parser.add_argument(
        "--astrbot-root",
        type=Path,
        help="optional path to a compatible AstrBot checkout",
    )
    args = parser.parse_args()
    tests = Path(__file__).resolve().parent
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.astrbot_root:
        env["ASTRBOT_ROOT"] = str(args.astrbot_root.resolve())

    commands = [
        ["-m", "unittest", "discover", "-s", str(tests), "-p", "test_heybox_reconciliation.py"],
        [str(tests / "check_heybox_atomic_race.py")],
    ]
    if args.vision:
        commands.extend(
            [
                ["-m", "unittest", "discover", "-s", str(tests), "-p", "test_heybox_vision.py"],
                [
                    "-m", "unittest", "discover", "-s", str(tests),
                    "-p", "test_heybox_vision_observation.py",
                ],
            ]
        )
    with tempfile.TemporaryDirectory(prefix="feiyu-offline-test-") as temp:
        for command in commands:
            print("Running", Path(command[-1]).name, flush=True)
            subprocess.run([sys.executable, "-B", *command], cwd=temp, env=env, check=True)


if __name__ == "__main__":
    main()
