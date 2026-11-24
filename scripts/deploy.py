"""Deploy built level to BeamNG user directory."""

import os
import shutil
import sys
import time

SRC_DIR = r"build/levels/ivanovo_mvp"
DST_DIR = r"F:/Games/BeamNG.drive/current/levels/ivanovo_mvp"
SRC_ZIP = r"build/ivanovo_mvp.zip"
DST_ZIP = r"F:/Games/BeamNG.drive/current/mods/ivanovo_mvp.zip"


def deploy() -> bool:
    if not os.path.exists(SRC_DIR):
        print(f"Error: {SRC_DIR} does not exist. Run build first.")
        return False

    print("Deploying unpacked level...")
    if os.path.exists(DST_DIR):
        shutil.rmtree(DST_DIR)
    shutil.copytree(SRC_DIR, DST_DIR)
    print(f"Successfully deployed level folder to {DST_DIR}")

    if os.path.exists(SRC_ZIP):
        os.makedirs(os.path.dirname(DST_ZIP), exist_ok=True)
        shutil.copy2(SRC_ZIP, DST_ZIP)
        print(f"Successfully copied mod archive to {DST_ZIP}")
    return True


if __name__ == "__main__":
    wait = "--wait" in sys.argv
    max_retries = 300 if wait else 1
    for attempt in range(max_retries):
        try:
            if deploy():
                print("Deployment completed successfully!")
                sys.exit(0)
        except PermissionError as exc:
            if wait:
                print(f"BeamNG is running / file locked ({exc}). Waiting... ({attempt + 1}/{max_retries})")
                time.sleep(2)
            else:
                print(f"Permission denied: {exc}")
                print("Please close BeamNG.drive and run: python scripts/deploy.py")
                sys.exit(1)
        except Exception as exc:
            print(f"Deployment error: {exc}")
            sys.exit(1)
