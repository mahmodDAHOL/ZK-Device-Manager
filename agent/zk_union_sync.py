#!/usr/bin/env python3
"""
ZK Union Sync — standalone runner (Windows version).

Reads users from all ZK devices, merges them into a union set,
then adds missing users to each device (never modifies existing ones).
Then does the same for user photos and face templates: a photo or face that
exists on any device is copied to every device that has the user but not it.
The face is what the device recognises people by; the photo is only the
picture it took while enrolling it.

Logs progress to ERPNext via the REST API into the "ZK Sync Log" doctype.

Usage:
    python zk_union_sync.py                          # real run
    python zk_union_sync.py --dry-run                # no writes to devices
    python zk_union_sync.py --users 1174,1371        # photos/faces for these users only
    python zk_union_sync.py --no-photos --no-faces   # users only
    python zk_union_sync.py --verify                 # read back each face after writing

Install:
    pip install pyzk requests pywin32

    Register the SDK once (Register_SDK_x64.bat as administrator, same bitness
    as Python). Without it, users sync fine but photos/faces are skipped.

ERPNext credentials can be given in the environment instead of this file:
    ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET

For Task Scheduler, e.g. every 30 minutes:
    Program:   C:\\Python311\\python.exe
    Arguments: C:\\zk_sync\\zk_union_sync.py
    Start in:  C:\\zk_sync
A second run that starts while one is still going exits at once.
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from zk import ZK

# Everything the script writes goes next to it, not into whatever directory it
# was started from — Task Scheduler starts jobs in C:\Windows\System32.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ================== ERPNext CONFIG ==================
ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "https://momc-erp.sy").rstrip("/")
API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")

if not API_KEY or not API_SECRET:
    print("ERPNEXT_API_KEY and ERPNEXT_API_SECRET must be set in the environment.")
    sys.exit(1)

session = requests.Session()
session.headers.update(
    {
        "Authorization": f"token {API_KEY}:{API_SECRET}",
        "Accept": "application/json",
    }
)

# ================== ZK CONFIG ==================
DEVICE_IPS = [
    "192.168.67.22",
    "192.168.67.11",
    "192.168.67.12",
    "192.168.67.13",
    "192.168.67.14",
    "192.168.67.15",
    "192.168.67.16",
]
PORT = 4370
PASSWORD = 0
CONNECT_TIMEOUT = 8
RETRIES = 2
MAX_WORKERS = 4

# Photos/faces touch the SDK through a COM object. Each worker thread creates
# its own SDK instance and calls pythoncom.CoInitialize() so it gets its own
# COM apartment. This lets the 7 devices be processed in parallel instead of
# serially — the single biggest win, since each device takes ~5 min to read.
BIO_WORKERS = 4

PHOTO_DIR = os.path.join(LOG_DIR, "photos")
FACE_DIR = os.path.join(LOG_DIR, "faces")
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")
SDK_MACHINE = 1
IS_WINDOWS = sys.platform.startswith("win")

# FACE_INDEX 50 asks for all of a user's face templates at once, as one string.
FACE_INDEX = 50


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zk-union-sync")


def _single_instance():
    """Holds a lock for the life of the process so two runs never write to the
    devices at once (Task Scheduler firing again while a slow run is still
    going)."""
    lock_path = os.path.join(LOG_DIR, "zk_union_sync.lock")
    handle = open(lock_path, "a+")
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("Another zk_union_sync run is still going; exiting.")
        sys.exit(0)
    return handle


_lock = _single_instance()

# Sanity check: confirm the token is valid and the user has permission
try:
    who = session.get(
        f"{ERPNEXT_URL}/api/method/frappe.auth.get_logged_user", timeout=15
    )
    who.raise_for_status()
    logged_user = who.json().get("message")
    if not logged_user or logged_user == "Guest":
        print("Token auth failed — received Guest. Check key/secret.")
        sys.exit(1)
    log.info(f"Authenticated as: {logged_user}")
    print(f"Authenticated as: {logged_user}")
except Exception as e:
    print(f"Auth check failed: {e}")
    sys.exit(1)


HEADERS = {
    "Authorization": f"token {API_KEY}:{API_SECRET}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------------- ERPNext helpers ----------------
def erp_create_log(**fields):
    """Create a ZK Sync Log record in ERPNext; return its name (or None)."""
    payload = {k: v for k, v in fields.items() if v is not None}
    try:
        r = requests.post(
            f"{ERPNEXT_URL}/api/resource/ZK Sync Log",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        if not r.ok:
            log.error("Create log failed HTTP %s: %s", r.status_code, r.text)
            return None
        r.raise_for_status()
        name = r.json()["data"]["name"]
        log.info("ERPNext log created: %s", name)
        return name
    except Exception as e:
        log.error(
            "Failed to create ERPNext log: %s — %s",
            e,
            getattr(e, "response", None) and e.response.text,
        )
        return None


def erp_update_log(name, **fields):
    """Update an existing ZK Sync Log record."""
    if not name:
        return
    payload = {k: v for k, v in fields.items() if v is not None}
    try:
        r = requests.put(
            f"{ERPNEXT_URL}/api/resource/ZK Sync Log/{name}",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        log.info("ERPNext log updated: %s", name)
    except Exception as e:
        log.error(
            "Failed to update ERPNext log %s: %s — %s",
            name,
            e,
            getattr(e, "response", None) and e.response.text,
        )


# ---------------- ZK helpers ----------------
def connect(ip):
    last = None
    for attempt in range(1, RETRIES + 1):
        zk = ZK(ip, port=PORT, timeout=CONNECT_TIMEOUT, password=PASSWORD)
        try:
            conn = zk.connect()
            conn.disable_device()
            return conn
        except Exception as e:
            last = e
            log.warning("connect %s attempt %d/%d: %s", ip, attempt, RETRIES, e)
            time.sleep(attempt)
    raise RuntimeError(f"Cannot connect to {ip}: {last}")


def close(conn):
    try:
        conn.enable_device()
    except Exception:
        pass
    try:
        conn.disconnect()
    except Exception:
        pass


def read_device(ip):
    """Returns (users_dict, existing_ids). The existing_ids set is cached so
    write_device does not have to re-read the same users."""
    log.info("READ %s", ip)
    conn = None
    try:
        conn = connect(ip)
        users = conn.get_users()
        log.info("  %s: %d users", ip, len(users))
        by_id = {str(u.user_id).strip(): {"user": u, "source": ip} for u in users}
        return by_id, set(by_id.keys())
    except Exception as e:
        log.error("  %s read failed: %s", ip, e)
        return {}, set()
    finally:
        if conn:
            close(conn)


def read_all_devices(ips, max_workers):
    """Read every device in parallel. Returns (union_set, existing_by_ip)."""
    merged = {}
    existing_by_ip = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(read_device, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                by_id, existing = fut.result()
                existing_by_ip[ip] = existing
                for uid, entry in by_id.items():
                    merged.setdefault(uid, entry)
            except Exception as e:
                log.error("read error %s: %s", ip, e)
                existing_by_ip[ip] = set()
    log.info("Union set: %d unique users", len(merged))
    return merged, existing_by_ip


def write_device(ip, union_set, existing, dry_run):
    """`existing` is the set of user IDs already on the device, from
    read_all_devices. No need to re-read."""
    conn = None
    res = {"ip": ip, "connected": False, "added": 0, "errors": 0, "missing": 0}
    try:
        conn = connect(ip)
        res["connected"] = True
        missing = [uid for uid in union_set if uid not in existing]
        res["missing"] = len(missing)

        if not missing:
            log.info("  %s: already complete", ip)
            return res

        log.info("  %s: adding %d missing users", ip, len(missing))
        for uid in missing:
            mu = union_set[uid]["user"]
            if dry_run:
                res["added"] += 1
                continue
            try:
                conn.set_user(
                    # None, not 0: pyzk only picks the next free slot when uid
                    # is None. With 0 every added user was written into the
                    # same slot, each one replacing the one before it.
                    uid=None,
                    user_id=mu.user_id,
                    name=mu.name,
                    privilege=mu.privilege,
                    password=mu.password,
                    group_id=mu.group_id,
                    card=mu.card,
                )
                res["added"] += 1
            except Exception as e:
                res["errors"] += 1
                log.warning("  %s: user %s failed: %s", ip, uid, e)

        log.info("  %s done: added=%d errors=%d", ip, res["added"], res["errors"])
        return res
    except Exception as e:
        res["error"] = str(e)
        log.error("  %s aborted: %s", ip, e)
        return res
    finally:
        if conn:
            close(conn)


def write_union_to_all(ips, union_set, existing_by_ip, dry_run, max_workers):
    summary = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                write_device, ip, union_set, existing_by_ip.get(ip, set()), dry_run
            ): ip
            for ip in ips
        }
        for fut in as_completed(futures):
            try:
                summary.append(fut.result())
            except Exception as e:
                summary.append({"ip": "unknown", "error": str(e)})
    return summary


# ---------------- User photos and face templates (ZKTeco SDK) ----------------
#
# pyzk cannot read or write user photos or face templates, so these go through
# zkemkeeper, the SDK's COM object.
#
# The COM object is apartment-threaded: a worker thread must call
# pythoncom.CoInitialize() before touching it and pythoncom.CoUninitialize()
# when done. Each worker thread creates its own SDK instance. This lets the 7
# devices be processed in parallel instead of serially.
#
# A photo on the device is a file named after the user: <user_id>.jpg.
# A face is what the device recognises people by; the photo is only the
# snapshot it took while enrolling that face. Both are copied.


def _sdk_available():
    """True if zkemkeeper is usable on this machine."""
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False, "pywin32 is not installed (pip install pywin32)"
    try:
        import win32com.client

        win32com.client.Dispatch("zkemkeeper.ZKEM.1")
        return True, None
    except Exception as e:
        return (
            False,
            f"SDK not registered for {64 if sys.maxsize > 2**32 else 32}-bit Python ({e})",
        )


def _new_sdk():
    """A fresh SDK object, COM-initialised for the calling thread."""
    import pythoncom

    pythoncom.CoInitialize()
    import win32com.client

    return win32com.client.Dispatch("zkemkeeper.ZKEM.1")


def _release_sdk():
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        pass


def _read_face(sdk, uid):
    """(template, length) for the user's faces on the connected device, or None."""
    try:
        got = sdk.GetUserFaceStr(SDK_MACHINE, str(uid), FACE_INDEX, "", 0)
    except Exception:
        return None
    # pywin32 hands back (result, template, length) for the by-ref arguments.
    if not got or not got[0]:
        return None
    template, length = got[1], got[-1]
    if not template or not length:
        return None
    return template, int(length)


def _sdk_connect(sdk, ip):
    for attempt in range(1, RETRIES + 1):
        if sdk.Connect_Net(ip, PORT):
            return True
        log.warning("sdk connect %s attempt %d/%d failed", ip, attempt, RETRIES)
        time.sleep(attempt)
    return False


def _photos_in(folder):
    """{user_id: path} for every photo file in folder."""
    photos = {}
    if not os.path.isdir(folder):
        return photos
    for file_name in os.listdir(folder):
        user_id, ext = os.path.splitext(file_name)
        path = os.path.join(folder, file_name)
        if ext.lower() in PHOTO_EXTENSIONS and os.path.getsize(path) > 0:
            photos[user_id.strip()] = path
    return photos


def read_device_bio(ip, user_ids, templates, do_photos, do_faces):
    """Reads one device's user photos and faces. Runs in a worker thread with
    its own SDK instance."""
    sdk = _new_sdk()
    if sdk is None or not _sdk_connect(sdk, ip):
        _release_sdk()
        log.error("  %s: photos/faces not read, SDK could not connect", ip)
        return None

    folder = os.path.join(PHOTO_DIR, ip.replace(".", "_"))
    os.makedirs(folder, exist_ok=True)
    # Last run's files would otherwise count as photos this device still has.
    for old in os.listdir(folder):
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass

    faces = set()
    try:
        sdk.EnableDevice(SDK_MACHINE, False)
        target = folder + os.sep
        for uid in user_ids:
            if do_photos:
                # GetAllUserPhoto answers True on this firmware but writes
                # nothing, so each user is asked for their photo by name.
                try:
                    sdk.DownloadUserPhoto(SDK_MACHINE, f"{uid}.jpg", target)
                except Exception:
                    pass  # this user has no photo on this device
            if do_faces:
                face = _read_face(sdk, uid)
                if face:
                    faces.add(uid)
                    if uid not in templates:
                        templates[uid] = face
                        _backup_face(uid, face, ip)
    finally:
        try:
            sdk.EnableDevice(SDK_MACHINE, True)
        finally:
            sdk.Disconnect()
            _release_sdk()

    photos = _photos_in(folder) if do_photos else {}
    log.info("  %s: %d user photos, %d faces", ip, len(photos), len(faces))
    return {"photos": photos, "faces": faces}


def _backup_face(uid, face, ip):
    """Keeps each face template that is about to be copied on disk too, so a
    face lost from every device can still be put back from logs/faces."""
    os.makedirs(FACE_DIR, exist_ok=True)
    template, length = face
    with open(os.path.join(FACE_DIR, f"{uid}.txt"), "w", encoding="utf-8") as f:
        f.write(
            f"# user {uid}, from {ip}, length {length}, {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        )
        f.write(template)


def write_device_bio(ip, photo_uploads, face_uploads, dry_run, verify):
    """Writes missing photos and faces to one device. Runs in a worker thread."""
    res = {
        "ip": ip,
        "photos_missing": len(photo_uploads),
        "photos_added": 0,
        "photo_errors": 0,
        "faces_missing": len(face_uploads),
        "faces_added": 0,
        "faces_verified": 0,
        "face_errors": 0,
    }
    if not photo_uploads and not face_uploads:
        log.info("  %s: photos and faces already complete", ip)
        return res
    if dry_run:
        res["photos_added"] = len(photo_uploads)
        res["faces_added"] = len(face_uploads)
        log.info(
            "  %s: would add %d photos, %d faces (e.g. users %s)",
            ip,
            len(photo_uploads),
            len(face_uploads),
            ", ".join(sorted(face_uploads)[:8]) or "-",
        )
        return res

    sdk = _new_sdk()
    if sdk is None or not _sdk_connect(sdk, ip):
        _release_sdk()
        res["bio_error"] = "SDK could not connect"
        log.error("  %s: photos/faces not written, SDK could not connect", ip)
        return res

    try:
        sdk.EnableDevice(SDK_MACHINE, False)
        log.info(
            "  %s: adding %d photos, %d faces",
            ip,
            len(photo_uploads),
            len(face_uploads),
        )
        for uid, (template, length) in face_uploads.items():
            try:
                if sdk.SetUserFaceStr(
                    SDK_MACHINE, str(uid), FACE_INDEX, template, length
                ):
                    res["faces_added"] += 1
                else:
                    res["face_errors"] += 1
                    log.warning(
                        "  %s: face for user %s refused (SDK code %s)",
                        ip,
                        uid,
                        sdk.GetLastError(0),
                    )
            except Exception as e:
                res["face_errors"] += 1
                log.warning("  %s: face for user %s failed: %s", ip, uid, e)
        for uid, path in photo_uploads.items():
            try:
                if sdk.UploadUserPhoto(SDK_MACHINE, path):
                    res["photos_added"] += 1
                else:
                    res["photo_errors"] += 1
                    log.warning("  %s: photo for user %s refused", ip, uid)
            except Exception as e:
                res["photo_errors"] += 1
                log.warning("  %s: photo for user %s failed: %s", ip, uid, e)
        try:
            sdk.RefreshData(SDK_MACHINE)
        except Exception:
            pass
        # Read-back doubles per-device time and almost always returns 0, so
        # it is off by default. Enable it with --verify.
        if verify:
            for uid in face_uploads:
                if _read_face(sdk, uid):
                    res["faces_verified"] += 1
    finally:
        try:
            sdk.EnableDevice(SDK_MACHINE, True)
        finally:
            sdk.Disconnect()
            _release_sdk()
    log.info(
        "  %s done: photos added=%d errors=%d | faces added=%d verified=%d errors=%d",
        ip,
        res["photos_added"],
        res["photo_errors"],
        res["faces_added"],
        res["faces_verified"],
        res["face_errors"],
    )
    return res


def sync_bio(
    ips,
    union_set,
    dry_run,
    do_photos=True,
    do_faces=True,
    only_users=None,
    verify=False,
    bio_workers=BIO_WORKERS,
):
    """Copies each user's photo and face to every device that lacks them.
    Devices are processed in parallel (BIO_WORKERS at a time), one SDK
    instance per worker thread."""
    ok, why = _sdk_available()
    if not ok:
        log.warning("PHOTOS/FACES skipped: %s", why)
        return None

    user_ids = [u for u in union_set if not only_users or u in only_users]
    templates = {}
    on_device = {}

    with ThreadPoolExecutor(max_workers=bio_workers) as pool:
        futures = {
            pool.submit(
                read_device_bio, ip, user_ids, templates, do_photos, do_faces
            ): ip
            for ip in ips
        }
        for fut in as_completed(futures):
            ip = futures[fut]
            log.info("READ PHOTOS/FACES %s", ip)
            try:
                on_device[ip] = fut.result()
            except Exception as e:
                log.error("  %s bio read failed: %s", ip, e)
                on_device[ip] = None

    # Where each user's photo can be taken from: the first device that has it.
    photo_source = {}
    for ip in ips:
        for uid, path in ((on_device[ip] or {}).get("photos") or {}).items():
            photo_source.setdefault(uid, path)
    log.info(
        "Union: %d users have a photo, %d have a face on some device",
        len(photo_source),
        len(templates),
    )

    summary = []
    with ThreadPoolExecutor(max_workers=BIO_WORKERS) as pool:
        futures = {}
        for ip in ips:
            held = on_device[ip]
            if held is None:
                summary.append(
                    {"ip": ip, "bio_error": "could not read device photos/faces"}
                )
                continue
            photo_uploads = {
                uid: path
                for uid, path in photo_source.items()
                if uid in union_set and uid not in held["photos"]
            }
            face_uploads = {
                uid: face
                for uid, face in templates.items()
                if uid in union_set and uid not in held["faces"]
            }
            futures[
                pool.submit(
                    write_device_bio, ip, photo_uploads, face_uploads, dry_run, verify
                )
            ] = ip
        for fut in as_completed(futures):
            try:
                summary.append(fut.result())
            except Exception as e:
                summary.append({"ip": futures[fut], "bio_error": str(e)})
    return summary


# ---------------- main ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="Only report, do not write to devices"
    )
    parser.add_argument(
        "--ips", default=None, help="Comma-separated IPs (overrides defaults)"
    )
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument(
        "--bio-workers",
        type=int,
        default=BIO_WORKERS,
        help="Parallel devices for photos/faces (default 4)",
    )
    parser.add_argument("--no-photos", action="store_true", help="Skip user photos")
    parser.add_argument("--no-faces", action="store_true", help="Skip face templates")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Read back each face after writing (slower)",
    )
    parser.add_argument(
        "--users",
        default=None,
        help="Comma-separated user IDs: copy photos/faces for these only",
    )
    args = parser.parse_args()
    only_users = (
        {u.strip() for u in args.users.split(",") if u.strip()} if args.users else None
    )

    ips = [x.strip() for x in args.ips.split(",")] if args.ips else DEVICE_IPS
    dry_run = args.dry_run
    max_workers = args.workers

    started_at = datetime.now()
    t0 = time.time()

    # 1. Create a "Running" log in ERPNext up front
    log_name = erp_create_log(
        status="Running",
        started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
        dry_run=1 if dry_run else 0,
        triggered_by=logged_user,
        devices=",".join(ips),
    )

    result = {
        "started": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "devices": ips,
        "dry_run": dry_run,
    }

    try:
        # 2. Read all devices (users + existing set, cached for step 3)
        union_set, existing_by_ip = read_all_devices(ips, max_workers)
        result["union_size"] = len(union_set)

        if not union_set:
            result["error"] = "No users found on any device."
            erp_update_log(
                log_name,
                status="Failed",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                error=result["error"],
                details=_dumps(result),
            )
            return

        # 3. Write union back to every device
        summary = write_union_to_all(
            ips, union_set, existing_by_ip, dry_run, max_workers
        )
        result["devices_result"] = summary
        result["total_added"] = sum(r.get("added", 0) for r in summary)

        # 4. Copy user photos and faces, in parallel across devices
        if args.no_photos and args.no_faces:
            result["photos"] = "skipped (--no-photos --no-faces)"
        else:
            bio_summary = sync_bio(
                ips,
                union_set,
                dry_run,
                do_photos=not args.no_photos,
                do_faces=not args.no_faces,
                only_users=only_users,
                verify=args.verify,
            )
            if bio_summary is None:
                result["photos"] = "skipped (ZKTeco SDK not available)"
            else:
                result["photos_result"] = bio_summary
                for key in (
                    "photos_added",
                    "photo_errors",
                    "faces_added",
                    "faces_verified",
                    "face_errors",
                ):
                    result[f"total_{key}"] = sum(r.get(key, 0) for r in bio_summary)

        result["elapsed_seconds"] = round(time.time() - t0, 1)

        # 5. Update the ERPNext log with the final outcome. Photo counts go in
        # `details` only: the ZK Sync Log doctype has no fields of its own for
        # them.
        erp_update_log(
            log_name,
            status="Success",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_seconds=result["elapsed_seconds"],
            union_size=result["union_size"],
            total_added=result["total_added"],
            total_errors=sum(r.get("errors", 0) for r in summary),
            details=_dumps(result),
        )

        log.info(
            "DONE — union=%d, users added=%d, photos added=%s, faces added=%s "
            "(verified on device: %s), elapsed=%.1fs%s",
            result["union_size"],
            result["total_added"],
            result.get("total_photos_added", result.get("photos")),
            result.get("total_faces_added", "-"),
            result.get("total_faces_verified", "-"),
            result["elapsed_seconds"],
            "  [DRY RUN: nothing was written]" if dry_run else "",
        )

    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        result["error"] = tb
        log.exception("Sync failed")
        erp_update_log(
            log_name,
            status="Failed",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            error=tb[:60000],
            details=_dumps(result),
        )
        sys.exit(1)


def _dumps(obj):
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


main()
