#!/usr/bin/env python3
"""ZK Device Manager agent.

Runs on a Windows PC on the devices' network (192.168.67.x). It asks ERPNext
for queued ZK Jobs, carries them out on the devices, and reports back what the
devices hold. ERPNext never connects to it; it only ever connects out.

    python zk_agent.py            # run for good (what run_agent.bat does)
    python zk_agent.py --once     # take one job, if any, and exit
    python zk_agent.py --refresh  # read every device into ERPNext now, then exit

Settings come from config.ini next to this file (see config.example.ini).
"""

import argparse
import configparser
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from zk_ops import ADMIN_PRIVILEGE  # or set ADMIN_PRIVILEGE = 14
from zk.exception import ZKErrorResponse  # <-- add at top of zk_agent.py
import requests

import zk_ops
from zk_ops import DeviceConnection, SdkConnection

VERSION = "0.1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("zk-agent")


def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    log.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "agent.log"),
        maxBytes=5_000_000,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(file_handler)
    log.addHandler(console)


# ---------------- config ----------------


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8")

    def get(section, key, env, default=""):
        return os.environ.get(env) or cfg.get(section, key, fallback=default)

    conf = {
        "url": get("erpnext", "url", "ERPNEXT_URL").rstrip("/"),
        "api_key": get("erpnext", "api_key", "ERPNEXT_API_KEY"),
        "api_secret": get("erpnext", "api_secret", "ERPNEXT_API_SECRET"),
        "name": get("agent", "name", "ZK_AGENT_NAME") or socket.gethostname(),
        "bio_workers": int(get("agent", "bio_workers", "ZK_AGENT_BIO_WORKERS", "4")),
        "sync_timeout_minutes": int(
            get("agent", "sync_timeout_minutes", "ZK_AGENT_SYNC_TIMEOUT", "240")
        ),
    }
    missing = [k for k in ("url", "api_key", "api_secret") if not conf[k]]
    if missing:
        sys.exit(f"Missing in config.ini [erpnext]: {', '.join(missing)}")
    return conf


# ---------------- ERPNext ----------------


class ERPNext:
    def __init__(self, conf):
        self.url = conf["url"]
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {conf['api_key']}:{conf['api_secret']}",
                "Accept": "application/json",
            }
        )

    def call(self, method, **kwargs):
        r = self.session.post(
            f"{self.url}/api/method/zk_device_manager.agent_api.{method}",
            json=kwargs,
            timeout=120,
        )
        if not r.ok:
            raise RuntimeError(f"{method}: HTTP {r.status_code}: {_server_message(r)}")
        return r.json().get("message")

    def upload_attachment(self, path, file_name, doctype, docname, fieldname):
        """Attaches a file to a record's field, as the attendance script always
        has: private, linked by doctype/docname/fieldname. Answers the File doc."""
        with open(path, "rb") as f:
            r = self.session.post(
                f"{self.url}/api/method/upload_file",
                files={"file": (file_name, f, "application/json")},
                data={
                    "is_private": 1,
                    "doctype": doctype,
                    "docname": docname,
                    "fieldname": fieldname,
                },
                timeout=120,
            )
        if not r.ok:
            raise RuntimeError(
                f"upload_file: HTTP {r.status_code}: {_server_message(r)}"
            )
        message = r.json().get("message")
        if not message:
            raise RuntimeError(f"upload_file answered no file: {r.text[:300]}")
        return message

    def attachments(self, doctype, docname, fieldname):
        filters = json.dumps(
            [
                ["attached_to_doctype", "=", doctype],
                ["attached_to_name", "=", docname],
                ["attached_to_field", "=", fieldname],
            ]
        )
        r = self.session.get(
            f"{self.url}/api/resource/File",
            params={
                "filters": filters,
                "fields": json.dumps(["name", "file_name"]),
                "limit_page_length": 0,
            },
            timeout=60,
        )
        if not r.ok:
            raise RuntimeError(f"File list: HTTP {r.status_code}: {_server_message(r)}")
        return r.json().get("data", [])

    def delete_file(self, name):
        r = self.session.delete(f"{self.url}/api/resource/File/{name}", timeout=60)
        if r.status_code not in (200, 202):
            raise RuntimeError(
                f"delete File {name}: HTTP {r.status_code}: {_server_message(r)}"
            )


def _server_message(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    for key in ("exception", "_server_messages", "message", "exc_type"):
        if body.get(key):
            return str(body[key])[:1000]
    return response.text[:500]


# ---------------- progress ----------------


class Progress:
    """Tells ERPNext how far the running job has got.

    Thread-safe (a refresh reads four devices at once), and sends at most once
    every INTERVAL seconds whatever the callers do, so reading 478 users does
    not become 478 requests. ERPNext works out the time left from it.
    """

    INTERVAL = 5

    def __init__(self, erp, job_name):
        self.erp, self.job = erp, job_name
        self.lock = threading.Lock()
        self.fraction, self.message, self.sent_at = 0.0, "", 0.0

    def set(self, fraction, message=None, force=False):
        with self.lock:
            self.fraction = max(self.fraction, min(max(fraction, 0.0), 1.0))
            if message:
                self.message = message
            due = force or time.monotonic() - self.sent_at >= self.INTERVAL
            if due:
                self.sent_at = time.monotonic()
            fraction, message = self.fraction, self.message
        if due:
            try:
                self.erp.call("report_progress", job=self.job, progress=round(fraction * 100, 1), message=message)
            except Exception as e:  # a lost progress report must never fail the job
                log.debug("progress report failed: %s", e)

    def span(self, lo, hi, parts=1):
        return Span(self, lo, hi, parts)


class Span:
    """One share of a job — from `lo` to `hi` of the whole — made of `parts`
    that finish on their own, in any order: the devices being read at once.
    Each part reports its own 0..1; the span is their average."""

    def __init__(self, progress, lo, hi, parts=1):
        self.progress, self.lo, self.hi = progress, lo, hi
        self.parts = max(parts, 1)
        self.done = {}
        self.lock = threading.Lock()

    def update(self, key, fraction, message=None):
        with self.lock:
            self.done[key] = min(max(fraction, 0.0), 1.0)
            share = sum(self.done.values()) / max(self.parts, len(self.done))
        self.progress.set(self.lo + (self.hi - self.lo) * share, message)

    def reporter(self, key):
        """A (fraction, message) callback for one part."""
        return lambda fraction, message=None: self.update(key, fraction, message)

    def finish(self, message=None):
        self.progress.set(self.hi, message, force=True)


class NoProgress:
    """What handlers get when nothing is listening, e.g. --refresh."""

    def set(self, *a, **k):
        pass

    def span(self, lo, hi, parts=1):
        return Span(self, lo, hi, parts)


class SyncProgress:
    """Follows zk_union_sync.py's own log lines while it runs.

    Each device goes through four steps, weighted by how long they really
    take: reading users and writing missing ones take seconds; reading every
    user's face and photo one at a time takes minutes (about 20 times as long);
    writing the missing faces and photos is in between. The script prints one
    line as each device finishes each step, and that is what is counted.
    """

    STEPS = (
        ("users read", re.compile(r"  (\S+): \d+ users$"), 1),
        ("users written", re.compile(r"  (\S+)(?:: already complete$| done: added=| aborted:)"), 1),
        ("faces and photos read", re.compile(r"  (\S+): (?:\d+ user photos, \d+ faces|photos/faces not read)"), 20),
        ("faces and photos written", re.compile(
            r"  (\S+)(?:: photos and faces already complete|: would add| done: photos added|: photos/faces not written)"), 4),
    )

    def __init__(self, span, ips, with_bio=True):
        self.span, self.ips = span, set(ips)
        self.steps = self.STEPS if with_bio else self.STEPS[:2]
        self.weight = {label: w for label, _, w in self.steps}
        self.total = sum(self.weight.values()) * max(len(ips), 1)
        self.seen = set()  # (step, ip) pairs already counted

    def feed(self, line):
        line = line.rstrip("\r\n")
        for label, pattern, _ in self.steps:
            m = pattern.search(line)
            if not m or m.group(1) not in self.ips or (label, m.group(1)) in self.seen:
                continue
            self.seen.add((label, m.group(1)))
            done = sum(self.weight[step] for step, _ in self.seen)
            count = sum(1 for step, _ in self.seen if step == label)
            self.span.update("sync", done / self.total, f"{label.capitalize()}: {count} of {len(self.ips)} devices")
            return


# ---------------- reading devices into ERPNext ----------------


def read_device_state(device, sdk_ok, only_users=None, report=None):
    """Everything one device holds, in the shape report_users takes.

    With `only_users`, only those users are read (after a job about one
    person); otherwise everyone (a refresh). Answers (info, entries).
    `report(fraction, message)` is told how far this one device has got.
    """
    report = report or (lambda *a, **k: None)
    report(0.0, f"Reading users on {device['name']}")
    with DeviceConnection(device["ip"], device["port"]) as conn:
        info = zk_ops.read_info(conn)
        users = zk_ops.users_by_id(conn)
        fps = zk_ops.fingerprint_counts(conn, users)
    wanted = [u for u in users if only_users is None or u in only_users]
    # Users and fingerprints come in one read; faces and photos are the slow
    # part, one user at a time, so they are most of the bar.
    report(0.05 if sdk_ok and wanted else 1.0, f"Read {len(users)} users on {device['name']}")
    entries = {
        uid: {
            "user_id": uid,
            "user_name": users[uid].name,
            "privilege": users[uid].privilege,
            "card_number": users[uid].card,
            "device_uid": users[uid].uid,
            "fingerprints": fps.get(uid, 0),
        }
        for uid in wanted
    }
    if sdk_ok and wanted:
        folder = zk_ops.device_photo_folder(device["ip"], "state")
        photos = 0
        with SdkConnection(device["ip"], device["port"]) as sdk:
            for n, uid in enumerate(wanted, 1):
                entries[uid]["has_face"] = 1 if sdk.read_face(uid) else 0
                path = sdk.download_photo(uid, folder)
                entries[uid]["has_photo"] = 1 if path else 0
                photos += 1 if path else 0
                if path:
                    os.remove(path)
                report(0.05 + 0.95 * n / len(wanted),
                       f"Reading faces and photos on {device['name']}: {n} of {len(wanted)} users")
        if only_users is None:
            info["photo_count"] = photos
    return info, list(entries.values())


def report_devices(erp, devices, sdk_ok, workers, only_users=None, span=None):
    """Reads each device (in parallel) and writes what it holds into ERPNext.
    Answers {device: short outcome}. `span` is the share of the job's progress
    bar this fills, one part per device."""
    outcome = {}
    if span is not None:
        span.parts = max(len(devices), 1)

    def one(device):
        report = span.reporter(device["name"]) if span is not None else None
        try:
            info, entries = read_device_state(device, sdk_ok, only_users, report)
        except Exception as e:
            if report:
                report(1.0, f"{device['name']} unreachable")
            erp.call("report_device", device=device["name"], error=str(e))
            return device["name"], f"unreachable: {e}"
        erp.call("report_device", device=device["name"], info=info)
        res = erp.call(
            "report_users",
            device=device["name"],
            users=entries,
            complete=0 if only_users else 1,
        )
        return device["name"], f"{len(entries)} users read ({res})"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in as_completed([pool.submit(one, d) for d in devices]):
            name, text = fut.result()
            outcome[name] = text
            log.info("  %s: %s", name, text)
    return outcome


# ---------------- jobs ----------------


class JobFailed(Exception):
    pass


class Agent:
    def __init__(self, conf):
        self.conf = conf
        self.erp = ERPNext(conf)
        self.progress = NoProgress()
        self.sdk_ok, self.sdk_why = zk_ops.sdk_available()
        if not self.sdk_ok:
            log.warning(
                "Faces and photos unavailable: %s. Users are still managed.",
                self.sdk_why,
            )

    # -- loop --

    def heartbeat(self):
        return self.erp.call(
            "heartbeat",
            host=f"{self.conf['name']} ({socket.gethostname()})",
            version=VERSION,
            sdk_available=1 if self.sdk_ok else 0,
        )

    def run_forever(self):
        poll = 30
        while True:
            try:
                poll = (self.heartbeat() or {}).get("poll_seconds", poll)
                while self.run_one():
                    pass
            except requests.RequestException as e:
                log.warning("ERPNext unreachable: %s", e)
            except Exception:
                log.exception("Agent loop error")
            time.sleep(poll)

    def run_one(self):
        """Takes and runs one job; answers whether there was one."""
        job = self.erp.call("claim_job", agent=self.conf["name"])
        if not job:
            return False
        log.info(
            "JOB %s: %s%s",
            job["name"],
            job["job_type"],
            " (dry run)" if job["dry_run"] else "",
        )
        for name in job.get("skipped_devices") or []:
            log.info("  skipping %s: disabled or unknown", name)
        handler = {
            "Sync All Devices": self.sync_all,
            "Refresh Device State": self.refresh,
            "Fetch Attendance": self.fetch_attendance,
            "Add User": self.add_user,
            "Update User": self.update_user,
            "Remove User": self.remove_user,
            "Copy Biometrics": self.copy_biometrics,
        }.get(job["job_type"])
        self.progress = Progress(self.erp, job["name"])
        try:
            if not handler:
                raise JobFailed(f"This agent does not know how to do {job['job_type']}")
            summary, result, sync_log = handler(job)
            status, error = "Done", None
            failed = [
                d
                for d, r in (result or {}).items()
                if isinstance(r, dict) and r.get("error")
            ]
            if failed and len(failed) == len(result):
                status = "Failed"
        except JobFailed as e:
            summary, result, sync_log, status, error = (
                str(e),
                None,
                None,
                "Failed",
                str(e),
            )
        except Exception as e:
            summary, result, sync_log, status = f"Failed: {e}", None, None, "Failed"
            error = traceback.format_exc()
        log.info("JOB %s %s: %s", job["name"], status, summary)
        self.progress = NoProgress()
        self.erp.call(
            "finish_job",
            job=job["name"],
            status=status,
            summary=summary,
            result=result,
            error=error,
            sync_log=sync_log,
        )
        return True

    # -- handlers: each answers (summary, result, sync_log) --

    def _each(self, devices, verb, lo=0.0, hi=1.0):
        """The devices one after another, moving the progress bar from `lo` to
        `hi` as each is reached and saying which one is being worked on."""
        n = max(len(devices), 1)
        for i, device in enumerate(devices):
            self.progress.set(lo + (hi - lo) * i / n, f"{verb} {device['name']} ({i + 1} of {len(devices)})", force=True)
            yield device
        self.progress.set(hi)

    def refresh(self, job):
        targets = job["targets"]
        outcome = report_devices(
            self.erp, targets, self.sdk_ok, self.conf["bio_workers"],
            span=self.progress.span(0.0, 1.0),
        )
        bad = [d for d, t in outcome.items() if t.startswith("unreachable")]
        summary = f"Read {len(targets) - len(bad)} of {len(targets)} device(s)"
        if bad:
            summary += f"; unreachable: {', '.join(bad)}"
        if not self.sdk_ok:
            summary += " (faces/photos not read: no SDK)"
        return summary, outcome, None

    def fetch_attendance(self, job):
        """What the middle server's attendance script did, as a job.

        Kept identical where the fingerprint app's Fetch Checkins reads it: one
        file per device named "<Company>_<slot>_<ip with _>_last_fetch_dump.json",
        attached privately to the Fingerprint record in the field
        "attach_<company lower, spaces as _>_<slot>_data", holding the records
        as attendance_json writes them.

        Different where the script could lose or stall data:
          * the new file is uploaded before the old one is deleted, so a
            failed upload leaves the last good file in place;
          * one device failing, or holding no records, no longer stops every
            other device from being uploaded;
          * a device is re-enabled even when reading it fails.
        """
        att = job.get("attendance") or {}
        company, doctype, docname = (
            att.get("company"),
            att.get("doctype") or "Fingerprint",
            att.get("docname"),
        )
        if not company or not docname:
            raise JobFailed(
                "Set the Company and the record to upload to, in ZK Settings > Attendance"
            )
        folder = os.path.join(LOG_DIR, "attendance")
        os.makedirs(folder, exist_ok=True)

        result = {}
        for device in self._each(job["targets"], "Fetching attendance from"):
            slot = device.get("attendance_slot")
            if not slot:
                result[device["name"]] = {
                    "skipped": "no attendance slot set on the ZK Device"
                }
                continue
            device_id = f"{company}_{slot}"
            file_name = (
                f"{device_id}_{device['ip'].replace('.', '_')}_last_fetch_dump.json"
            )
            fieldname = f"attach_{device_id}_data".lower().replace(" ", "_")
            try:
                records = zk_ops.read_attendance(device["ip"], device["port"])
                r = {"records": len(records), "field": fieldname}
                if not records:
                    # Nothing to send; the file already on the record stays.
                    r["upload"] = "skipped, no records on the device"
                elif job["dry_run"]:
                    r["upload"] = "would upload"
                else:
                    path = os.path.join(folder, file_name)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(zk_ops.attendance_json(records))
                    new = self.erp.upload_attachment(
                        path, file_name, doctype, docname, fieldname
                    )
                    old = [
                        x["name"]
                        for x in self.erp.attachments(doctype, docname, fieldname)
                        if x["name"] != new.get("name")
                    ]
                    for name in old:
                        self.erp.delete_file(name)
                    r["upload"] = f"uploaded {new.get('file_url')}, replaced {len(old)}"
                result[device["name"]] = r
            except Exception as e:
                result[device["name"]] = {"error": str(e)}
            log.info("  %s: %s", device["name"], result[device["name"]])

        done = [
            d
            for d, r in result.items()
            if str(r.get("upload", "")).startswith(("uploaded", "would"))
        ]
        failed = [d for d, r in result.items() if r.get("error")]
        summary = f"{'[DRY RUN] ' if job['dry_run'] else ''}Attendance from {len(done)} device(s)"
        total = sum(r.get("records", 0) for r in result.values())
        summary += f", {total} records"
        if failed:
            summary += f"; failed: {', '.join(failed)}"
        return summary, result, None

    def sync_all(self, job):
        """Runs zk_union_sync.py unchanged, so the sync still writes its own
        ZK Sync Log record exactly as before."""
        script = os.path.join(BASE_DIR, "zk_union_sync.py")
        args = [
            sys.executable,
            script,
            "--ips",
            ",".join(d["ip"] for d in job["targets"]),
        ]
        if job["dry_run"]:
            args.append("--dry-run")
        if not job["copy_faces"]:
            args.append("--no-faces")
        if not job["copy_photos"]:
            args.append("--no-photos")
        env = dict(
            os.environ,
            ERPNEXT_URL=self.conf["url"],
            ERPNEXT_API_KEY=self.conf["api_key"],
            ERPNEXT_API_SECRET=self.conf["api_secret"],
            PYTHONIOENCODING="utf-8",
            # Line by line, not in 8 KB blocks, or the bar would only move
            # every few devices.
            PYTHONUNBUFFERED="1",
        )
        log.info("  running %s", " ".join(args[1:]))
        out_path = os.path.join(LOG_DIR, f"sync_{job['name']}.log")
        refresh_after = job["settings"].get("refresh_after_sync") and not job["dry_run"]
        # The sync is most of the bar; re-reading the devices afterwards, the rest.
        tracker = SyncProgress(
            self.progress.span(0.0, 0.8 if refresh_after else 1.0),
            [d["ip"] for d in job["targets"]],
            with_bio=bool(job["copy_faces"] or job["copy_photos"]),
        )
        # Its output is read line by line as it runs, both to follow its
        # progress and to keep the full log; a timer stops it if it overruns.
        with open(out_path, "w", encoding="utf-8") as out:
            proc = subprocess.Popen(
                args,
                cwd=BASE_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            timed_out = threading.Event()

            def stop():
                timed_out.set()
                proc.kill()

            timer = threading.Timer(self.conf["sync_timeout_minutes"] * 60, stop)
            timer.start()
            try:
                for line in proc.stdout:
                    out.write(line)
                    tracker.feed(line)
                proc.wait()
            finally:
                timer.cancel()
        if timed_out.is_set():
            raise JobFailed(
                f"The sync ran past {self.conf['sync_timeout_minutes']} minutes and was stopped"
            )
        text = open(out_path, encoding="utf-8", errors="replace").read()
        sync_log = _first(r"ERPNext log created: (\S+)", text)
        done = _first(r"DONE — (.+)", text)
        if proc.returncode != 0 or not done:
            tail = "\n".join(text.strip().splitlines()[-15:])
            raise JobFailed(f"The sync failed (exit {proc.returncode}):\n{tail}")
        result = {"sync_output": out_path}
        if refresh_after:
            log.info("  refreshing device state after the sync")
            result["refresh"] = report_devices(
                self.erp, job["targets"], self.sdk_ok, self.conf["bio_workers"],
                span=self.progress.span(0.8, 1.0),
            )
        return done, result, sync_log

    def add_user(self, job):
        user = job["user"]
        uid = user["user_id"]
        result = {}
        for device in self._each(job["targets"], "Adding to", 0.0, 0.2):
            try:
                if job["dry_run"]:
                    with DeviceConnection(device["ip"], device["port"]) as conn:
                        there = uid in zk_ops.users_by_id(conn)
                    result[device["name"]] = {"would": "update" if there else "add"}
                    continue
                with DeviceConnection(device["ip"], device["port"]) as conn:
                    users = zk_ops.users_by_id(conn)
                    result[device["name"]] = {
                        "user": zk_ops.put_user(conn, users, user)
                    }
            except Exception as e:
                result[device["name"]] = {"error": str(e)}
        written = [d for d in job["targets"] if "user" in result.get(d["name"], {})]
        if any([job["copy_faces"], job["copy_photos"], job["copy_fingerprints"]]) and (
            written or job["dry_run"]
        ):
            copied = self._copy_bio(
                job, written if not job["dry_run"] else job["targets"], 0.2, 0.8
            )
            for name, r in copied.items():
                result.setdefault(name, {}).update(r)
        if not job["dry_run"]:
            self._report_user(job["targets"], uid, 0.8, 1.0)
        return (
            _summary(f"Add {user['user_name']} ({uid})", result, job["dry_run"]),
            result,
            None,
        )

    def update_user(self, job):
        user = job["user"]
        uid = user["user_id"]
        result = {}
        for device in self._each(job["targets"], "Updating on", 0.0, 0.7):
            try:
                with DeviceConnection(device["ip"], device["port"]) as conn:
                    users = zk_ops.users_by_id(conn)
                    if uid not in users:
                        result[device["name"]] = {"skipped": "not on this device"}
                    elif job["dry_run"]:
                        result[device["name"]] = {"would": "update"}
                    else:
                        result[device["name"]] = {
                            "user": zk_ops.put_user(conn, users, user)
                        }
            except Exception as e:
                result[device["name"]] = {"error": str(e)}
        if not job["dry_run"]:
            self._report_user(job["targets"], uid, 0.7, 1.0)
        return (
            _summary(f"Update {user['user_name']} ({uid})", result, job["dry_run"]),
            result,
            None,
        )

    def remove_user(self, job):
        user = job["user"]
        uid = str(user["user_id"])
        result = {}
        for device in self._each(job["targets"], "Removing from"):
            try:
                backup = None

                # 1. Back up the face first (SDK).
                if self.sdk_ok and not job["dry_run"]:
                    with SdkConnection(device["ip"], device["port"]) as sdk:
                        face = sdk.read_face(uid)
                        if face:
                            backup = zk_ops.backup_face(
                                uid, face, device["ip"], reason="removed"
                            )

                if job["dry_run"]:
                    with DeviceConnection(device["ip"], device["port"]) as conn:
                        users = zk_ops.users_by_id(conn)
                        if uid not in users:
                            result[device["name"]] = {"skipped": "not on this device"}
                        else:
                            result[device["name"]] = {"would": "remove"}
                else:
                    # 2. Delete via SDK — handles attendance records.
                    deleted = False
                    if self.sdk_ok:
                        with SdkConnection(device["ip"], device["port"]) as sdk:
                            # Re-check presence using pyzk first (cheap, reliable).
                            with DeviceConnection(device["ip"], device["port"]) as conn:
                                if uid not in zk_ops.users_by_id(conn):
                                    result[device["name"]] = {
                                        "skipped": "not on this device"
                                    }
                                    continue
                            deleted = sdk.delete_user(uid)

                    # 3. Fallback to pyzk (for non-Windows or when SDK is unavailable).
                    if not deleted:
                        with DeviceConnection(device["ip"], device["port"]) as conn:
                            users = zk_ops.users_by_id(conn)
                            if uid not in users:
                                result[device["name"]] = {
                                    "skipped": "not on this device"
                                }
                                continue
                            try:
                                conn.delete_user(user_id=uid)
                                deleted = True
                            except Exception as exc:
                                # 4. Last resort: clear attendance, then retry.
                                #    Firmware 6.60 refuses with code 4992 when the
                                #    user has punch records. Clearing removes them.
                                log.info(
                                    "JOB %s: delete failed (%s) — clearing attendance and retrying",
                                    job["name"],
                                    exc,
                                )
                                try:
                                    conn.clear_attendance()
                                    conn.delete_user(user_id=uid)
                                    deleted = True
                                except Exception:
                                    # Re-read: the firmware sometimes reports
                                    # failure but deletes anyway.
                                    time.sleep(1)
                                    if uid in zk_ops.users_by_id(conn):
                                        raise
                                    log.info(
                                        "JOB %s: %s is gone after the error — treating as success",
                                        job["name"],
                                        uid,
                                    )
                                    deleted = True

                    if deleted:
                        result[device["name"]] = {
                            "user": "removed",
                            "face_backup": backup,
                        }
                        if result[device["name"]].get("user") == "removed":
                            self.erp.call(
                                "report_users",
                                device=device["name"],
                                users=[],
                                removed=[uid],
                            )
                    else:
                        result[device["name"]] = {"error": "delete returned false"}
            except Exception as e:
                result[device["name"]] = {"error": str(e)}
        return (
            _summary(f"Remove {user['user_name']} ({uid})", result, job["dry_run"]),
            result,
            None,
        )

    def copy_biometrics(self, job):
        user = job["user"]
        result = self._copy_bio(job, job["targets"], 0.0, 0.8)
        if not job["dry_run"]:
            self._report_user(job["targets"], user["user_id"], 0.8, 1.0)
        return (
            _summary(
                f"Copy {user['user_name']} ({user['user_id']})", result, job["dry_run"]
            ),
            result,
            None,
        )

    # -- helpers --

    def _copy_bio(self, job, targets, lo=0.0, hi=1.0):
        """Copies the user's face, photo and fingerprints to each target that
        lacks them, from the named source or any device that has them. Never
        replaces what a target already has. Moves the progress bar from `lo`
        to `hi`: looking for them first, then writing them."""
        mid = lo + (hi - lo) * 0.4
        uid = job["user"]["user_id"]
        want_face = job["copy_faces"] and self.sdk_ok
        want_photo = job["copy_photos"] and self.sdk_ok
        want_fp = job["copy_fingerprints"]
        target_names = {d["name"] for d in targets}
        sources = (
            [job["source"]]
            if job.get("source")
            else (
                [d for d in job["all_devices"] if d["name"] not in target_names]
                + [d for d in job["all_devices"] if d["name"] in target_names]
            )
        )

        face = photo = None
        fingers = []
        found = {}
        for device in self._each(sources, "Looking for the face on", lo, mid):
            if not (
                (want_face and not face)
                or (want_photo and not photo)
                or (want_fp and not fingers)
            ):
                break
            try:
                if (want_face and not face) or (want_photo and not photo):
                    with SdkConnection(device["ip"], device["port"]) as sdk:
                        if want_face and not face:
                            face = sdk.read_face(uid)
                            if face:
                                found["face"] = device["name"]
                                zk_ops.backup_face(
                                    uid, face, device["ip"], reason="copy"
                                )
                        if want_photo and not photo:
                            photo = sdk.download_photo(
                                uid, zk_ops.device_photo_folder(device["ip"], "copy")
                            )
                            if photo:
                                found["photo"] = device["name"]
                if want_fp and not fingers:
                    with DeviceConnection(device["ip"], device["port"]) as conn:
                        users = zk_ops.users_by_id(conn)
                        if uid in users:
                            fingers = zk_ops.user_fingerprints(conn, users[uid])
                            if fingers:
                                found["fingerprints"] = device["name"]
            except Exception as e:
                log.warning(
                    "  %s: could not read %s from it: %s", device["name"], uid, e
                )

        result = {}
        for device in self._each(targets, "Copying to", mid, hi):
            r = {"found_on": found} if found else {"found_on": "nowhere"}
            try:
                # A face or photo for someone the device does not have would
                # belong to nobody there. Add User puts the person on first.
                with DeviceConnection(device["ip"], device["port"]) as conn:
                    present = uid in zk_ops.users_by_id(conn)
                if not present and not job["dry_run"]:
                    r["skipped"] = "user not on this device (use Add User)"
                    result[device["name"]] = r
                    continue
                if (face or photo) and self.sdk_ok:
                    with SdkConnection(device["ip"], device["port"]) as sdk:
                        if face:
                            if sdk.read_face(uid):
                                r["face"] = "already there"
                            elif job["dry_run"]:
                                r["face"] = "would copy"
                            else:
                                ok = sdk.write_face(uid, face) and bool(
                                    sdk.read_face(uid)
                                )
                                r["face"] = (
                                    "copied"
                                    if ok
                                    else f"refused (SDK code {sdk.last_error()})"
                                )
                        if photo:
                            folder = zk_ops.device_photo_folder(device["ip"], "check")
                            there = sdk.download_photo(uid, folder)
                            if there:
                                os.remove(there)
                                r["photo"] = "already there"
                            elif job["dry_run"]:
                                r["photo"] = "would copy"
                            else:
                                r["photo"] = (
                                    "copied" if sdk.upload_photo(photo) else "refused"
                                )
                if fingers:
                    with DeviceConnection(device["ip"], device["port"]) as conn:
                        users = zk_ops.users_by_id(conn)
                        if uid not in users:
                            r["fingerprints"] = (
                                "would copy after adding"
                                if job["dry_run"]
                                else "user not on this device"
                            )
                        elif zk_ops.user_fingerprints(conn, users[uid]):
                            r["fingerprints"] = "already there"
                        elif job["dry_run"]:
                            r["fingerprints"] = f"would copy {len(fingers)}"
                        else:
                            conn.save_user_template(users[uid], fingers)
                            r["fingerprints"] = f"copied {len(fingers)}"
            except Exception as e:
                r["error"] = str(e)
            result[device["name"]] = r
        return result

    def _report_user(self, targets, uid, lo=0.0, hi=1.0):
        try:
            report_devices(
                self.erp,
                targets,
                self.sdk_ok,
                self.conf["bio_workers"],
                only_users={uid},
                span=self.progress.span(lo, hi),
            )
        except Exception as e:
            log.warning("  could not report %s back: %s", uid, e)


def _first(pattern, text):
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None


def _summary(what, result, dry_run):
    parts = []
    for device, r in result.items():
        if r.get("error"):
            parts.append(f"{device}: failed ({r['error']})")
            continue
        bits = [
            f"{k} {v}"
            for k, v in r.items()
            if k in ("user", "would", "skipped", "face", "photo", "fingerprints")
        ]
        parts.append(f"{device}: {', '.join(bits) or 'nothing to do'}")
    prefix = "[DRY RUN] " if dry_run else ""
    return f"{prefix}{what} — " + "; ".join(parts)


def single_instance():
    """Two agents on one PC would claim jobs from each other's hands."""
    handle = open(os.path.join(LOG_DIR, "zk_agent.lock"), "a+")
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another zk_agent is already running on this computer.")
    return handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="Run at most one job, then exit"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Read every enabled device into ERPNext, then exit",
    )
    args = parser.parse_args()

    setup_logging()
    _lock = single_instance()  # noqa: F841 — held for the life of the process
    agent = Agent(load_config())
    cfg = agent.heartbeat()
    log.info(
        "Agent %s %s connected to %s; %d device(s); faces/photos %s",
        agent.conf["name"],
        VERSION,
        agent.conf["url"],
        len(cfg.get("devices") or []),
        "on" if agent.sdk_ok else f"off ({agent.sdk_why})",
    )
    if args.refresh:
        report_devices(
            agent.erp, cfg.get("devices") or [], agent.sdk_ok, agent.conf["bio_workers"]
        )
        return
    if args.once:
        agent.run_one()
        return
    agent.run_forever()


if __name__ == "__main__":
    main()
