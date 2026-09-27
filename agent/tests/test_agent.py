"""Runs every agent job type against simulated devices and a simulated ERPNext.

No device and no ERPNext needed, only pyzk installed (pip install -r agent/requirements.txt):

    python agent/tests/test_agent.py
"""
import copy
import os
import shutil
import sys
import tempfile

AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT)

import zk_ops  # noqa: E402

TMP = tempfile.mkdtemp()
zk_ops.FACE_BACKUP_DIR = os.path.join(TMP, "faces")
zk_ops.PHOTO_WORK_DIR = os.path.join(TMP, "photos")

import zk_agent  # noqa: E402


class User:
    def __init__(self, uid, user_id, name, privilege=0, password="", group_id="", card=0):
        self.uid, self.user_id, self.name = uid, user_id, name
        self.privilege, self.password, self.group_id, self.card = privilege, password, group_id, card


class Finger:
    def __init__(self, uid, fid, template):
        self.uid, self.fid, self.valid, self.template = uid, fid, 1, template


def device(users, faces=None, photos=None, fps=None):
    return {"users": {u.user_id: u for u in users}, "faces": faces or {}, "photos": photos or {}, "fps": fps or {}}


DEVICES = {}


class FakeConn:
    def __init__(self, ip, port=4370, password=0, timeout=8):
        self.d = DEVICES[ip]
        self.users, self.users_cap, self.fingers, self.fingers_cap = 0, 10000, 0, 4000
        self.faces, self.faces_cap, self.records, self.rec_cap = 0, 3000, 0, 100000

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read_sizes(self):
        self.users = len(self.d["users"])
        self.faces = len(self.d["faces"])
        self.fingers = sum(len(v) for v in self.d["fps"].values())

    def get_device_name(self): return "iFace880 plus"
    def get_serialnumber(self): return "SN"
    def get_platform(self): return "ZMM720_TFT"
    def get_firmware_version(self): return "6.60"
    def get_face_version(self): return 12
    def get_fp_version(self): return 10

    def get_users(self):
        return list(self.d["users"].values())

    def set_user(self, uid=None, name="", privilege=0, password="", group_id="", user_id="", card=0):
        if uid is None:
            uid = max([u.uid for u in self.d["users"].values()] or [0]) + 1
        for existing in list(self.d["users"].values()):
            if existing.uid == uid and existing.user_id != user_id:
                raise AssertionError("slot reused")
        self.d["users"][user_id] = User(uid, user_id, name, privilege, password, group_id, card)

    def delete_user(self, uid=0, user_id=""):
        self.d["users"].pop(user_id)
        self.d["faces"].pop(user_id, None)
        self.d["fps"].pop(user_id, None)

    def get_templates(self):
        out = []
        for user_id, fingers in self.d["fps"].items():
            u = self.d["users"][user_id]
            out += [Finger(u.uid, f.fid, f.template) for f in fingers]
        return out

    def get_user_template(self, uid, temp_id=0, user_id=""):
        for user_id, u in self.d["users"].items():
            if u.uid == uid:
                for f in self.d["fps"].get(user_id, []):
                    if f.fid == temp_id:
                        return f
        return None

    def save_user_template(self, user, fingers):
        self.d["fps"][user.user_id] = [Finger(user.uid, f.fid, f.template) for f in fingers]


class FakeSdk:
    def __init__(self, ip, port=4370):
        self.d = DEVICES[ip]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def last_error(self): return -100

    def read_face(self, user_id):
        t = self.d["faces"].get(str(user_id))
        return (t, len(t)) if t else None

    def write_face(self, user_id, face):
        if str(user_id) not in self.d["users"]:
            raise AssertionError("face written for a user the device does not have")
        self.d["faces"][str(user_id)] = face[0]
        return True

    def download_photo(self, user_id, folder):
        os.makedirs(folder, exist_ok=True)
        if str(user_id) not in self.d["photos"]:
            return None
        path = os.path.join(folder, f"{user_id}.jpg")
        open(path, "wb").write(self.d["photos"][str(user_id)])
        return path

    def upload_photo(self, path):
        uid = os.path.splitext(os.path.basename(path))[0]
        self.d["photos"][uid] = open(path, "rb").read()
        return True


zk_agent.DeviceConnection = FakeConn
zk_agent.SdkConnection = FakeSdk


class FakeERP:
    def __init__(self):
        self.calls, self.queue, self.finished = [], [], []

    def call(self, method, **kw):
        self.calls.append((method, kw))
        if method == "claim_job":
            return self.queue.pop(0) if self.queue else None
        if method == "finish_job":
            self.finished.append(kw)
        if method == "heartbeat":
            return {"poll_seconds": 30, "devices": []}
        return {"ok": 1}


def D(name, ip):
    return {"name": name, "ip": ip, "port": 4370, "enabled": 1}


A, B, C = D("A", "10.0.0.1"), D("B", "10.0.0.2"), D("C", "10.0.0.3")


def job(job_type, targets, user_id="1174", **kw):
    j = {
        "name": f"ZKJ-{job_type}", "job_type": job_type, "dry_run": 0,
        "copy_faces": 1, "copy_photos": 1, "copy_fingerprints": 1,
        "user": {"user_id": user_id, "user_name": "Rana", "privilege": 0, "card_number": "", "status": "Active"},
        "targets": targets, "skipped_devices": [], "source": None, "all_devices": [A, B, C],
        "settings": {"refresh_after_sync": 1},
    }
    j.update(kw)
    return j


def reset():
    DEVICES.clear()
    DEVICES["10.0.0.1"] = device(
        [User(1, "1", "Admin", 14), User(2, "1174", "Rana", password="1234")],
        faces={"1174": "FACE-1174"}, photos={"1174": b"JPG"},
        fps={"1174": [Finger(2, 0, b"FP0"), Finger(2, 6, b"FP6")]},
    )
    DEVICES["10.0.0.2"] = device([User(1, "1", "Admin", 14), User(5, "999", "Other")])
    DEVICES["10.0.0.3"] = device([User(1, "1", "Admin", 14), User(7, "1174", "Rana")], faces={"1174": "OWN-C-FACE"})


def agent():
    a = zk_agent.Agent.__new__(zk_agent.Agent)
    a.conf = {"name": "test", "bio_workers": 2, "url": "x", "api_key": "k", "api_secret": "s", "sync_timeout_minutes": 1}
    a.erp = FakeERP()
    a.sdk_ok, a.sdk_why = True, None
    return a


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    assert cond, label


# 1. Add User to B, dry run first: nothing written.
reset(); a = agent(); before = copy.deepcopy(DEVICES)
a.erp.queue = [job("Add User", [B], dry_run=1)]
a.run_one()
check("dry-run Add User writes nothing", {k: (set(v["users"]), v["faces"], v["photos"]) for k, v in DEVICES.items()}
      == {k: (set(v["users"]), v["faces"], v["photos"]) for k, v in before.items()})
check("dry-run Add User finishes Done", a.erp.finished[-1]["status"] == "Done")
print("   ", a.erp.finished[-1]["summary"])

# 2. Add User to B for real: user, face, photo and both fingerprints arrive; own slot.
a.erp.queue = [job("Add User", [B])]
a.run_one()
b = DEVICES["10.0.0.2"]
check("user added to B", "1174" in b["users"] and b["users"]["1174"].uid == 6)
check("face copied to B from A", b["faces"].get("1174") == "FACE-1174")
check("photo copied to B", b["photos"].get("1174") == b"JPG")
check("2 fingerprints copied to B", [f.template for f in b["fps"].get("1174", [])] == [b"FP0", b"FP6"])
check("B's other user untouched", b["users"]["999"].name == "Other")
check("state reported back for the user", any(m == "report_users" and kw["device"] == "B" and kw["complete"] == 0 for m, kw in a.erp.calls))
print("   ", a.erp.finished[-1]["summary"])

# 3. Copy Biometrics to C, which has its own face: kept; photo and fingerprints added.
a.erp.queue = [job("Copy Biometrics", [C])]
a.run_one()
c = DEVICES["10.0.0.3"]
check("C keeps its own face", c["faces"]["1174"] == "OWN-C-FACE")
check("C gets the photo", c["photos"].get("1174") == b"JPG")
check("C gets the fingerprints", len(c["fps"].get("1174", [])) == 2)
print("   ", a.erp.finished[-1]["summary"])

# 4. Copy to a device without the user: skipped, nothing written for nobody.
a.erp.queue = [job("Copy Biometrics", [B], user_id="1", user={"user_id": "777", "user_name": "Ghost", "privilege": 0, "card_number": "", "status": "Active"})]
reset(); a.run_one()
check("copy to device lacking the user is skipped", "777" not in DEVICES["10.0.0.2"]["faces"])

# 5. Update User: name changes, device password kept, slot kept.
reset(); a = agent()
a.erp.queue = [job("Update User", [A, B], user={"user_id": "1174", "user_name": "Rana Haddad", "privilege": 14, "card_number": "555", "status": "Active"})]
a.run_one()
u = DEVICES["10.0.0.1"]["users"]["1174"]
check("A: name/privilege/card updated", (u.name, u.privilege, u.card) == ("Rana Haddad", 14, 555))
check("A: password and slot kept", (u.password, u.uid) == ("1234", 2))
check("B: not on it, skipped", "1174" not in DEVICES["10.0.0.2"]["users"])
print("   ", a.erp.finished[-1]["summary"])

# 6. Remove User from A: face backed up first, user gone, ERPNext told.
reset(); a = agent()
a.erp.queue = [job("Remove User", [A], copy_faces=0, copy_photos=0, copy_fingerprints=0)]
a.run_one()
check("A: user removed", "1174" not in DEVICES["10.0.0.1"]["users"])
backups = os.listdir(os.path.join(zk_ops.FACE_BACKUP_DIR, "removed"))
check("face backed up before removal", len(backups) == 1 and "FACE-1174" in open(os.path.join(zk_ops.FACE_BACKUP_DIR, "removed", backups[0])).read())
check("ERPNext told it was removed", any(m == "report_users" and kw.get("removed") == ["1174"] for m, kw in a.erp.calls))

# 7. Refresh: full report with face/photo/fingerprint presence.
reset(); a = agent()
a.erp.queue = [job("Refresh Device State", [A, B, C], user=None)]
a.run_one()
reports = {kw["device"]: kw for m, kw in a.erp.calls if m == "report_users"}
rana_a = next(u for u in reports["A"]["users"] if u["user_id"] == "1174")
check("refresh reports complete lists", all(r["complete"] == 1 for r in reports.values()))
check("refresh reads face/photo/fingerprints", (rana_a["has_face"], rana_a["has_photo"], rana_a["fingerprints"]) == (1, 1, 2))
devinfo = next(kw for m, kw in a.erp.calls if m == "report_device" and kw["device"] == "A")
check("refresh reports device info + photo count", devinfo["info"]["model"] == "iFace880 plus" and devinfo["info"]["photo_count"] == 1)

# 8. Unreachable device: reported offline, job still finishes.
reset(); a = agent()
bad = D("X", "10.9.9.9")
a.erp.queue = [job("Refresh Device State", [A, bad], user=None)]
a.run_one()
check("unreachable device reported with an error", any(m == "report_device" and kw["device"] == "X" and kw.get("error") for m, kw in a.erp.calls))
check("job finishes Done with a partial result", a.erp.finished[-1]["status"] == "Done" and "unreachable: X" in a.erp.finished[-1]["summary"])

# 9. No SDK: users still managed, faces/photos left alone.
reset(); a = agent(); a.sdk_ok = False
a.erp.queue = [job("Add User", [B])]
a.run_one()
check("no SDK: user still added", "1174" in DEVICES["10.0.0.2"]["users"])
check("no SDK: no face written", "1174" not in DEVICES["10.0.0.2"]["faces"])
check("no SDK: fingerprints still copied (pyzk)", len(DEVICES["10.0.0.2"]["fps"].get("1174", [])) == 2)

# 10. Fetch Attendance: the same files, fields and JSON as the old script.
import datetime as _dt  # noqa: E402
import json as _json  # noqa: E402
from zk.attendance import Attendance  # noqa: E402

RECORDS = {
    "10.0.0.1": [Attendance("1174", _dt.datetime(2026, 9, 27, 8, 1, 5), 1, 0, 2),
                 Attendance("1", _dt.datetime(2026, 9, 27, 8, 3, 0), 15, 0, 1)],
    "10.0.0.2": [],
    "10.0.0.3": [Attendance("1174", _dt.datetime(2026, 9, 27, 16, 0, 0), 1, 1, 7)],
}
FakeConn.get_attendance = lambda self: RECORDS[[ip for ip, d in DEVICES.items() if d is self.d][0]]
zk_ops.DeviceConnection = FakeConn


class FileERP(FakeERP):
    def __init__(self, fail_on=None):
        super().__init__()
        self.files, self.fail_on, self.n = {}, fail_on, 0

    def upload_attachment(self, path, file_name, doctype, docname, fieldname):
        if self.fail_on and self.fail_on in file_name:
            raise RuntimeError("HTTP 500")
        self.n += 1
        name = f"F{self.n}"
        self.files[name] = {"name": name, "file_name": file_name, "field": (doctype, docname, fieldname),
                            "content": open(path, encoding="utf-8").read()}
        return {"name": name, "file_url": f"/private/files/{file_name}"}

    def attachments(self, doctype, docname, fieldname):
        return [f for f in self.files.values() if f["field"] == (doctype, docname, fieldname)]

    def delete_file(self, name):
        self.files.pop(name)


def old_script_output(company, n, ip, records):
    """What the middle server's attendance script writes, verbatim."""
    device_id = f"{company}_{n}"
    return (
        f"{device_id}_{ip.replace('.', '_')}_last_fetch_dump.json",
        f"attach_{device_id}_data".lower().replace(" ", "_"),
        _json.dumps(list(map(lambda x: x.__dict__, records)), default=_dt.datetime.timestamp),
    )


ATT = {"company": "Ministry of Information", "doctype": "Fingerprint", "docname": "eg2g83k1ar"}
SA, SB, SC = dict(A, attendance_slot=2), dict(B, attendance_slot=3), dict(C, attendance_slot=4)
reset(); a = agent(); a.erp = FileERP()
a.erp.files["OLD"] = {"name": "OLD", "file_name": "old.json", "field": ("Fingerprint", "eg2g83k1ar", "attach_ministry_of_information_2_data"), "content": "[]"}
a.erp.queue = [job("Fetch Attendance", [SA, SB, SC, dict(D("N", "10.0.0.1"))], user=None, attendance=ATT)]
a.run_one()
fname, field, content = old_script_output("Ministry of Information", 2, "10.0.0.1", RECORDS["10.0.0.1"])
up_a = [f for f in a.erp.files.values() if f["field"][2] == field]
check("A: same field as the old script", field == "attach_ministry_of_information_2_data" and len(up_a) == 1)
check("A: same file name as the old script", up_a[0]["file_name"] == fname)
check("A: byte-identical JSON", up_a[0]["content"] == content)
check("A: the previous file was replaced", "OLD" not in a.erp.files)
check("B: empty device skipped, others still uploaded", not [f for f in a.erp.files.values() if "_3_" in f["file_name"]]
      and any("_4_" in f["file_name"] for f in a.erp.files.values()))
check("device without a slot skipped", "no attendance slot" in str(_json.loads(_json.dumps(a.erp.finished[-1]["result"]))["N"]))
check("job Done", a.erp.finished[-1]["status"] == "Done")
print("   ", a.erp.finished[-1]["summary"])

# 11. Upload fails: the last good file stays.
reset(); a = agent(); a.erp = FileERP(fail_on="_2_")
a.erp.files["OLD"] = {"name": "OLD", "file_name": "old.json", "field": ("Fingerprint", "eg2g83k1ar", "attach_ministry_of_information_2_data"), "content": "[]"}
a.erp.queue = [job("Fetch Attendance", [SA, SC], user=None, attendance=ATT)]
a.run_one()
check("failed upload keeps the previous file", "OLD" in a.erp.files)
check("failed device reported, the other still uploaded", "failed: A" in a.erp.finished[-1]["summary"]
      and any("_4_" in f["file_name"] for f in a.erp.files.values()))

# 12. Dry run uploads nothing.
reset(); a = agent(); a.erp = FileERP()
a.erp.queue = [job("Fetch Attendance", [SA], user=None, attendance=ATT, dry_run=1)]
a.run_one()
check("dry-run attendance uploads nothing", a.erp.files == {})

shutil.rmtree(TMP, ignore_errors=True)
print("ALL AGENT TESTS PASSED")
