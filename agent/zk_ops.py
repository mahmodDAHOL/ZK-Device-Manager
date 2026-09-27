"""Everything the agent does to a device.

Users, their details and fingerprints go through pyzk (pure Python, port 4370).
Faces and user photos go through ZKTeco's Standalone SDK (zkemkeeper, Windows
only), because pyzk has no commands for them. What was learned about the
site's devices (iFace880 plus, firmware 6.60) and is relied on here:

  * GetAllUserPhoto answers True and writes nothing, so photos are read one
    user at a time with DownloadUserPhoto("<user_id>.jpg"); any other name
    ("1174", "1174.JPG") is refused.
  * GetUserFaceStr(user_id, 50) answers all of a user's face templates as one
    string, and SetUserFaceStr takes it back. Every device runs the same face
    algorithm (12), so a template from one is valid on the others.
  * The photo is the snapshot a device takes while enrolling a face: copying it
    shows the picture, copying the face is what lets the person punch in.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

from zk import ZK

log = logging.getLogger("zk-agent")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
FACE_BACKUP_DIR = os.path.join(LOG_DIR, "faces")
PHOTO_WORK_DIR = os.path.join(LOG_DIR, "photos_work")

CONNECT_TIMEOUT = 8
RETRIES = 2
SDK_MACHINE = 1
FACE_INDEX = 50  # all of a user's face templates, as one string
FINGER_IDS = range(10)
ADMIN_PRIVILEGE = 14


# ---------------- pyzk ----------------

class DeviceConnection:
    """A pyzk connection with the device disabled while it is held, so nobody
    punches in half-way through a write."""

    def __init__(self, ip, port=4370, password=0, timeout=CONNECT_TIMEOUT):
        self.ip, self.port, self.password, self.timeout = ip, port, password, timeout
        self.conn = None

    def __enter__(self):
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                self.conn = ZK(
                    self.ip, port=self.port, timeout=self.timeout, password=self.password
                ).connect()
                self.conn.disable_device()
                return self.conn
            except Exception as e:  # pyzk raises bare Exceptions
                last = e
                log.warning("connect %s attempt %d/%d: %s", self.ip, attempt, RETRIES, e)
                time.sleep(attempt)
        raise ConnectionError(f"Cannot connect to {self.ip}:{self.port}: {last}")

    def __exit__(self, *exc):
        for step in ("enable_device", "disconnect"):
            try:
                getattr(self.conn, step)()
            except Exception:
                pass
        return False


def _safe(fn, default=None):
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def read_info(conn):
    """The device's details and counts, in ZK Device's field names."""
    conn.read_sizes()
    return {
        "model": _safe(conn.get_device_name, ""),
        "serial_number": _safe(conn.get_serialnumber, ""),
        "platform": _safe(conn.get_platform, ""),
        "firmware": _safe(conn.get_firmware_version, ""),
        "face_algorithm": str(_safe(conn.get_face_version, "")),
        "fingerprint_algorithm": str(_safe(conn.get_fp_version, "")),
        "user_count": conn.users,
        "face_count": conn.faces,
        "fingerprint_count": conn.fingers,
        "record_count": conn.records,
        "user_capacity": conn.users_cap,
        "face_capacity": conn.faces_cap,
        "fingerprint_capacity": conn.fingers_cap,
    }


def users_by_id(conn):
    """{user_id: pyzk User} for everyone on the device."""
    return {str(u.user_id).strip(): u for u in conn.get_users()}


def fingerprint_counts(conn, users):
    """{user_id: number of fingerprints}, from one bulk read of every template."""
    by_uid = {u.uid: uid for uid, u in users.items()}
    counts = {}
    for finger in _safe(conn.get_templates, []) or []:
        user_id = by_uid.get(finger.uid)
        if user_id is not None:
            counts[user_id] = counts.get(user_id, 0) + 1
    return counts


def user_fingerprints(conn, user):
    """The pyzk Finger objects one user has on the device."""
    fingers = []
    for fid in FINGER_IDS:
        finger = _safe(lambda: conn.get_user_template(uid=user.uid, temp_id=fid))
        if finger and getattr(finger, "template", None):
            fingers.append(finger)
    return fingers


def put_user(conn, users, fields):
    """Adds the user, or changes the details of the one already there.

    `fields` is {user_id, user_name, privilege, card_number}. Answers "added"
    or "updated". The device's own password for the user is kept.
    """
    uid = str(fields["user_id"])
    current = users.get(uid)
    common = dict(
        name=(fields.get("user_name") or uid),
        privilege=int(fields.get("privilege") or 0),
        user_id=uid,
        card=int(fields.get("card_number") or 0) if str(fields.get("card_number") or "0").isdigit() else 0,
    )
    if current:
        conn.set_user(
            uid=current.uid,
            password=current.password,
            group_id=current.group_id,
            **common,
        )
        return "updated"
    # None, not 0: pyzk picks the next free slot only for None. With 0 every
    # added user lands in the same slot, each replacing the one before.
    conn.set_user(uid=None, password="", group_id="", **common)
    return "added"


def read_attendance(ip, port=4370):
    """Every attendance record on the device, as pyzk Attendance objects.

    30 seconds a packet, as the middle server's own attendance script used:
    a device holding 16,000 records answers slowly. The device is re-enabled
    however the read ends — the old script left it disabled when it failed.
    """
    with DeviceConnection(ip, port, timeout=30) as conn:
        return conn.get_attendance()


def attendance_json(records):
    """The records exactly as the fingerprint app's Fetch Checkins has always
    received them: each record's fields (uid, user_id, timestamp, status,
    punch) with the timestamp as Unix seconds in this computer's timezone."""
    return json.dumps(
        [r.__dict__ for r in records],
        default=datetime.timestamp,
    )


# ---------------- ZKTeco SDK ----------------

def sdk_available():
    """(True, None) when faces and photos can be handled here, else (False, why)."""
    if not sys.platform.startswith("win"):
        return False, "the ZKTeco SDK exists for Windows only"
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            win32com.client.Dispatch("zkemkeeper.ZKEM.1")
        finally:
            pythoncom.CoUninitialize()
        return True, None
    except ImportError:
        return False, "pywin32 is not installed (pip install pywin32)"
    except Exception as e:
        bits = 64 if sys.maxsize > 2**32 else 32
        return False, f"the ZKTeco SDK is not registered for {bits}-bit Python ({e})"


class SdkConnection:
    """The SDK connected to one device, in the calling thread's own COM
    apartment, with the device disabled while it is held."""

    def __init__(self, ip, port=4370):
        self.ip, self.port = ip, port
        self.sdk = None

    def __enter__(self):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self.sdk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
        for attempt in range(1, RETRIES + 1):
            if self.sdk.Connect_Net(self.ip, self.port):
                self.sdk.EnableDevice(SDK_MACHINE, False)
                return self
            log.warning("sdk connect %s attempt %d/%d failed", self.ip, attempt, RETRIES)
            time.sleep(attempt)
        self._release()
        raise ConnectionError(f"SDK cannot connect to {self.ip}:{self.port}")

    def __exit__(self, *exc):
        try:
            self.sdk.RefreshData(SDK_MACHINE)
        except Exception:
            pass
        try:
            self.sdk.EnableDevice(SDK_MACHINE, True)
        except Exception:
            pass
        try:
            self.sdk.Disconnect()
        except Exception:
            pass
        self._release()
        return False

    @staticmethod
    def _release():
        try:
            import pythoncom

            pythoncom.CoUninitialize()
        except Exception:
            pass

    def last_error(self):
        got = _safe(lambda: self.sdk.GetLastError(0))
        return got[-1] if isinstance(got, tuple) else got

    def read_face(self, user_id):
        """(template, length) for the user's faces, or None when they have none."""
        got = _safe(lambda: self.sdk.GetUserFaceStr(SDK_MACHINE, str(user_id), FACE_INDEX, "", 0))
        # pywin32 answers (result, template, length) for the by-ref arguments.
        if not got or not got[0] or not got[1] or not got[-1]:
            return None
        return got[1], int(got[-1])

    def write_face(self, user_id, face):
        template, length = face
        return bool(_safe(lambda: self.sdk.SetUserFaceStr(SDK_MACHINE, str(user_id), FACE_INDEX, template, length), False))

    def download_photo(self, user_id, folder):
        """Saves the user's photo into folder as <user_id>.jpg; answers its path or None."""
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{user_id}.jpg")
        if os.path.exists(path):
            os.remove(path)
        _safe(lambda: self.sdk.DownloadUserPhoto(SDK_MACHINE, f"{user_id}.jpg", folder + os.sep))
        return path if os.path.exists(path) and os.path.getsize(path) > 0 else None

    def upload_photo(self, path):
        """The file must be named <user_id>.jpg: the device takes the user from the name."""
        return bool(_safe(lambda: self.sdk.UploadUserPhoto(SDK_MACHINE, path), False))


def backup_face(user_id, face, ip, reason="copy"):
    """Every face template the agent copies or removes is also kept on disk, so
    one lost from every device can still be put back."""
    folder = os.path.join(FACE_BACKUP_DIR, reason)
    os.makedirs(folder, exist_ok=True)
    template, length = face
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(folder, f"{user_id}_{ip.replace('.', '_')}_{stamp}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# user {user_id}, from {ip}, length {length}, {stamp}\n")
        f.write(template)
    return path


def device_photo_folder(ip, purpose):
    folder = os.path.join(PHOTO_WORK_DIR, purpose, ip.replace(".", "_"))
    os.makedirs(folder, exist_ok=True)
    return folder
