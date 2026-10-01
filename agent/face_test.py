"""Copy missing faces to every device that lacks them, from any device that has them.

    python face_batch_copy.py 192.168.67.11,192.168.67.12,192.168.67.13
    python face_batch_copy.py 192.168.67.11,192.168.67.12 --per-index
    python face_batch_copy.py 192.168.67.11,192.168.67.12 --dry-run

For each device, finds users without a face on that device, finds another
device that has the face, and writes it to the target (only if that user
still has no face there). Backups in logs/faces/ are used as a fallback.

Reads the whole face list once per device, so it's fast even with hundreds
of users. Nothing is ever replaced: a face that exists on the target is
left alone."""

import argparse
import glob
import os
import re
import sys
import time

import pythoncom
import win32com.client

MACHINE = 1
ALL = 50
FACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "faces")


# ---------------------------------------------------------------- helpers


def code(sdk):
    got = sdk.GetLastError(0)
    return got[-1] if isinstance(got, tuple) else got


def connect(ip):
    sdk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
    ok = sdk.Connect_Net(ip, 4370)
    if not ok:
        raise RuntimeError(f"connect {ip} failed (code {code(sdk)})")
    return sdk


def get_users(sdk):
    """All user_ids on the device via SSR_GetAllUserInfo loop."""
    users = []
    sdk.ReadAllUserID(MACHINE)
    while True:
        got = sdk.SSR_GetAllUserInfo(MACHINE)
        if not isinstance(got, tuple) or not got[0]:
            break
        # got = (ok, user_id, name, password, privilege, enabled)
        users.append(str(got[1]).strip())
    return users


def has_face(sdk, uid):
    """True if the device holds a face template for this user."""
    got = sdk.GetUserFaceStr(MACHINE, uid, ALL, "", 0)
    return bool(isinstance(got, tuple) and got[0] and got[1] and got[-1])


def read_face(sdk, uid):
    """(template, length) or None."""
    got = sdk.GetUserFaceStr(MACHINE, uid, ALL, "", 0)
    if isinstance(got, tuple) and got[0] and got[1]:
        return got[1], int(got[-1])
    return None


def write_face(sdk, uid, template, length, per_index=False, pieces=None):
    """Write the face; try index 50 first, then per-index if asked."""
    sdk.EnableDevice(MACHINE, False)
    try:
        ok = bool(sdk.SetUserFaceStr(MACHINE, uid, ALL, template, length))
        if not ok and per_index and pieces:
            for i, (t, l) in sorted(pieces.items()):
                r = sdk.SetUserFaceStr(MACHINE, uid, i, t, l)
                if r:
                    ok = True
        sdk.RefreshData(MACHINE)
        return ok
    finally:
        sdk.EnableDevice(MACHINE, True)


def read_all_face_indexes(sdk, uid):
    """Dict {index: (template, length)} for indexes 0..14 (rarely needed)."""
    pieces = {}
    for i in range(15):
        got = sdk.GetUserFaceStr(MACHINE, uid, i, "", 0)
        if isinstance(got, tuple) and got[0] and got[1] and got[-1]:
            pieces[i] = (got[1], int(got[-1]))
    return pieces


def index_backups(dirs):
    """{uid: [backup paths, newest first]} across every backup folder.

    Two layouts are read: <dir>/<uid>.txt (zk_union_sync.py) and
    <dir>/<reason>/<uid>_<ip>_<time>.txt (the agent). Scanned once, so a
    device with hundreds of users missing does not glob hundreds of times."""
    found = {}
    for d in dirs:
        for path in glob.glob(os.path.join(d, "*.txt")):
            uid = os.path.splitext(os.path.basename(path))[0]
            found.setdefault(uid, []).append(path)
        for path in glob.glob(os.path.join(d, "*", "*_*.txt")):
            uid = os.path.basename(path).split("_", 1)[0]
            found.setdefault(uid, []).append(path)
    for paths in found.values():
        paths.sort(key=os.path.getmtime, reverse=True)
    return found


def from_backup(uid, backups):
    """(template, length, path) from the newest saved backup of this user, or None."""
    for path in backups.get(uid, []):
        with open(path, encoding="utf-8") as f:
            header = f.readline()
            template = f.read().strip()
        if not template:
            continue
        m = re.search(r"length (\d+)", header)
        length = int(m.group(1)) if m else len(template)
        return template, length, path
    return None


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("ips", help="comma-separated list of device IPs")
    ap.add_argument(
        "--per-index",
        action="store_true",
        help="also try per-index writes when index 50 is refused",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be copied, write nothing",
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="show per-user detail")
    ap.add_argument(
        "--backup-dir",
        action="append",
        default=[],
        help="a folder of face backups to fall back on (repeatable); "
        "e.g. C:\\Users\\S I N O\\Downloads\\ZK_BACKUP_2026-09-29\\faces. "
        "logs\\faces next to this script is always searched too.",
    )
    args = ap.parse_args()

    ips = [ip.strip() for ip in args.ips.split(",") if ip.strip()]
    if not ips:
        sys.exit("no IPs given")

    backup_dirs = [d for d in args.backup_dir + [FACE_DIR] if os.path.isdir(d)]
    backups = index_backups(backup_dirs)
    print(f"Face backups: {len(backups)} users in {backup_dirs or 'no folder found'}")
    for d in args.backup_dir:
        if not os.path.isdir(d):
            print(f"  (not a folder, ignored: {d})")

    pythoncom.CoInitialize()

    # ---- 1. Read every device's user list and face status once ----
    print(f"Reading {len(ips)} device(s)...")
    state = {}  # ip -> {"users": set, "with_face": set, "faces": {uid: (t,l)}}
    for ip in ips:
        try:
            sdk = connect(ip)
        except Exception as e:
            print(f"  {ip}: SKIP — {e}")
            continue
        try:
            users = set(get_users(sdk))
            print(f"  {ip}: {len(users)} users", end="", flush=True)
            # Sample user_ids we might need to read faces for later; don't
            # read all faces upfront — that's slow. We only read the face
            # of a user when we actually need to copy it.
            state[ip] = {"users": users, "sdk_ready": False}
        finally:
            sdk.Disconnect()

    if not state:
        sys.exit("No devices reachable.")

    # ---- 2. Compute the union of all users ----
    all_users = set().union(*(s["users"] for s in state.values()))
    print(f"\nUnion of all users: {len(all_users)}")

    # ---- 3. For each device, find which users lack a face ----
    # We must read each device's face list. That's slow, but only done once.
    print("\nReading faces on each device (this takes time)...")
    for ip, info in state.items():
        sdk = connect(ip)
        try:
            with_face = set()
            faces = {}
            users = sorted(info["users"])
            for i, uid in enumerate(users, 1):
                got = read_face(sdk, uid)
                if got:
                    with_face.add(uid)
                    faces[uid] = got
                if args.verbose and i % 50 == 0:
                    print(f"  {ip}: {i}/{len(users)}", end="\r", flush=True)
            info["with_face"] = with_face
            info["faces"] = faces
            missing = info["users"] - with_face
            print(f"  {ip}: {len(with_face)} faces, {len(missing)} missing")
        finally:
            sdk.Disconnect()

    # ---- 4. Copy missing faces ----
    print("\nCopying missing faces...\n")
    total_written = 0
    total_failed = 0
    total_skipped = 0

    for target_ip, tinfo in state.items():
        missing = sorted(tinfo["users"] - tinfo["with_face"])
        if not missing:
            print(f"{target_ip}: nothing missing")
            continue

        print(f"{target_ip}: {len(missing)} users need a face")

        # Open the target connection once and reuse it
        try:
            target = connect(target_ip)
        except Exception as e:
            print(f"  cannot open target: {e}")
            continue

        try:
            for uid in missing:
                # Find a source that has this user's face
                source = None
                face = None
                pieces = None

                for src_ip, sinfo in state.items():
                    if src_ip == target_ip:
                        continue
                    if uid in sinfo["with_face"]:
                        source = src_ip
                        face = sinfo["faces"][uid]
                        break

                # Fallback: on no device at all, so from the saved backups
                if not face:
                    saved = from_backup(uid, backups)
                    if saved:
                        source = f"backup:{saved[2]}"
                        face = (saved[0], saved[1])

                if not face:
                    if args.verbose:
                        print(f"  {uid}: no source")
                    total_skipped += 1
                    continue

                if args.dry_run:
                    print(
                        f"  {uid}: would copy from {source} "
                        f"(template {len(face[0])} chars, length {face[1]})"
                    )
                    continue

                # Write it
                ok = write_face(
                    target,
                    uid,
                    face[0],
                    face[1],
                    per_index=args.per_index,
                    pieces=pieces,
                )
                if ok and has_face(target, uid):
                    if args.verbose or True:  # always show successful copies
                        print(f"  {uid}: copied from {source} ✓")
                    total_written += 1
                else:
                    print(f"  {uid}: FAILED to write (from {source})")
                    total_failed += 1

        finally:
            target.Disconnect()

    print(
        f"\nDone — written={total_written}, failed={total_failed}, "
        f"no source={total_skipped}"
    )


if __name__ == "__main__":
    main()
