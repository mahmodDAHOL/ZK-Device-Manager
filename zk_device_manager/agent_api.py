"""What the agent on the office network calls.

The site cannot reach the devices — they are on 192.168.67.x with no gateway —
so it never calls the agent either. The agent asks: every few seconds it says
it is alive (heartbeat), takes the oldest queued job (claim_job), does it,
reports what the devices now hold (report_device / report_users), and closes
the job (finish_job).

Every method is for the agent's own API user only: what it reports is written
as the truth about who can open which door.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from zk_device_manager.install import AGENT_ROLE

# pyzk's privilege numbers: 0 is a plain user, 14 an administrator.
ADMIN_PRIVILEGE = 14

DEVICE_INFO_FIELDS = (
	"model",
	"serial_number",
	"platform",
	"firmware",
	"face_algorithm",
	"fingerprint_algorithm",
	"user_count",
	"face_count",
	"fingerprint_count",
	"photo_count",
	"record_count",
	"user_capacity",
	"face_capacity",
	"fingerprint_capacity",
)


def _only_agent():
	frappe.only_for([AGENT_ROLE, "System Manager"])


def _loads(value, default=None):
	if value is None or value == "":
		return default
	if isinstance(value, (dict, list)):
		return value
	return json.loads(value)


def _device(name):
	row = frappe.db.get_value(
		"ZK Device", name, ["name", "ip_address", "port", "enabled"], as_dict=True
	)
	if not row:
		return None
	return {"name": row.name, "ip": row.ip_address, "port": cint(row.port) or 4370, "enabled": cint(row.enabled)}


def _enabled_devices():
	return [
		{"name": d.name, "ip": d.ip_address, "port": cint(d.port) or 4370, "enabled": 1}
		for d in frappe.get_all(
			"ZK Device",
			filters={"enabled": 1},
			fields=["name", "ip_address", "port"],
			order_by="device_name asc",
		)
	]


@frappe.whitelist(methods=["POST"])
def heartbeat(host=None, version=None, sdk_available=0):
	"""Records that the agent is alive and answers its settings and devices."""
	_only_agent()
	frappe.db.set_single_value(
		"ZK Settings",
		{
			"last_heartbeat": now_datetime(),
			"agent_host": (host or "")[:140],
			"agent_version": (version or "")[:140],
			"sdk_available": cint(sdk_available),
		},
	)
	settings = frappe.get_single("ZK Settings")
	return {
		"poll_seconds": max(cint(settings.poll_seconds) or 30, 10),
		"devices": _enabled_devices(),
		"server_time": str(now_datetime()),
	}


@frappe.whitelist(methods=["POST"])
def claim_job(agent=None):
	"""Takes the oldest queued job and answers everything needed to do it, or
	None when there is nothing to do.

	Row-locked, so two agents started by mistake cannot both take one job.
	"""
	_only_agent()
	rows = frappe.db.sql(
		"""select name from `tabZK Job` where status = 'Queued'
		order by creation asc limit 1 for update""",
		as_dict=True,
	)
	if not rows:
		return None
	job = frappe.get_doc("ZK Job", rows[0].name)
	job.db_set(
		{
			"status": "Running",
			"claimed_by": (agent or "")[:140],
			"started_at": now_datetime(),
		},
		commit=True,
	)
	return _payload(job)


def _payload(job):
	settings = frappe.get_single("ZK Settings")
	names = [row.device for row in job.target_devices]
	if not names and job.job_type == "Update User" and job.zk_user:
		names = frappe.get_all(
			"ZK Device User", filters={"zk_user": job.zk_user}, pluck="device"
		)
	targets, skipped = [], []
	if names:
		for name in names:
			device = _device(name)
			if device and device["enabled"]:
				targets.append(device)
			else:
				skipped.append(name)
	else:
		targets = _enabled_devices()

	user = None
	if job.zk_user:
		u = frappe.get_doc("ZK User", job.zk_user)
		user = {
			"user_id": u.user_id,
			"user_name": u.user_name,
			"privilege": ADMIN_PRIVILEGE if u.privilege == "Admin" else 0,
			"card_number": u.card_number or "",
			"status": u.status,
		}

	return {
		"name": job.name,
		"job_type": job.job_type,
		"dry_run": cint(job.dry_run),
		"copy_faces": cint(job.copy_faces),
		"copy_photos": cint(job.copy_photos),
		"copy_fingerprints": cint(job.copy_fingerprints),
		"user": user,
		"targets": targets,
		"skipped_devices": skipped,
		"source": _device(job.source_device) if job.source_device else None,
		# Where a user's face may be found when no source is named.
		"all_devices": _enabled_devices(),
		"settings": {
			"sync_copy_faces": cint(settings.sync_copy_faces),
			"sync_copy_photos": cint(settings.sync_copy_photos),
			"refresh_after_sync": cint(settings.refresh_after_sync),
		},
	}


@frappe.whitelist(methods=["POST"])
def finish_job(job, status, summary=None, result=None, error=None, sync_log=None):
	_only_agent()
	if status not in ("Done", "Failed"):
		frappe.throw(_("A job finishes as Done or Failed, not {0}.").format(status))
	doc = frappe.get_doc("ZK Job", job)
	doc.db_set(
		{
			"status": status,
			"finished_at": now_datetime(),
			"summary": (summary or "")[:2000],
			"result": result if isinstance(result, str) or result is None else json.dumps(result, indent=1, default=str),
			"error": error,
			"sync_log": sync_log,
		},
		commit=True,
	)
	return doc.name


@frappe.whitelist(methods=["POST"])
def report_device(device, info=None, error=None):
	"""What the agent found on one device: its details and counts, or the
	error that kept it from reading it."""
	_only_agent()
	if not frappe.db.exists("ZK Device", device):
		frappe.throw(_("No ZK Device {0}").format(device))
	values = {"last_seen": now_datetime()} if not error else {}
	if error:
		values.update({"status": "Offline", "last_error": str(error)[:2000]})
	else:
		values.update({"status": "Online", "last_error": None})
		for key, value in (_loads(info, {}) or {}).items():
			if key in DEVICE_INFO_FIELDS and value is not None:
				values[key] = value
	frappe.db.set_value("ZK Device", device, values, update_modified=False)
	return values.get("status")


@frappe.whitelist(methods=["POST"])
def report_users(device, users, complete=0, removed=None):
	"""Who is on one device, and what each of them has there.

	`users` is a list of {user_id, user_name, privilege, card_number,
	device_uid, has_face, fingerprints, has_photo}; a value left out or null
	means "not read this time" and keeps what was known. With `complete`, the
	list is everyone on the device and anyone missing from it is gone from it.
	`removed` names users taken off the device by a job.
	"""
	_only_agent()
	if not frappe.db.exists("ZK Device", device):
		frappe.throw(_("No ZK Device {0}").format(device))
	users = _loads(users, []) or []
	removed = [str(u) for u in (_loads(removed, []) or [])]
	now = now_datetime()

	existing = {
		row.user_id: row
		for row in frappe.get_all(
			"ZK Device User",
			filters={"device": device},
			fields=["name", "user_id", "zk_user", "user_name", "privilege", "card_number",
				"device_uid", "has_face", "fingerprints", "has_photo"],
		)
	}
	known_users = set(frappe.get_all("ZK User", pluck="name"))
	linked_employees = set(
		frappe.get_all("ZK User", filters={"employee": ("is", "set")}, pluck="employee")
	)
	employee_by_device_id = {
		str(e.attendance_device_id).strip(): e.name
		for e in frappe.get_all(
			"Employee",
			filters={"attendance_device_id": ("is", "set")},
			fields=["name", "attendance_device_id"],
		)
	}

	created = updated = inserted = 0
	problems = []
	seen = set()
	for u in users:
		uid = str(u.get("user_id") or "").strip()
		if not uid:
			continue
		seen.add(uid)
		if uid not in known_users:
			try:
				_create_zk_user(uid, u, employee_by_device_id, linked_employees)
				known_users.add(uid)
				created += 1
			except Exception as e:
				problems.append(f"{uid}: {e}")
				frappe.clear_last_message()

		values = {
			"zk_user": uid if uid in known_users else None,
			"user_name": u.get("user_name"),
			"privilege": "Admin" if cint(u.get("privilege")) == ADMIN_PRIVILEGE else "User",
			"card_number": str(u.get("card_number") or "") or None,
			"device_uid": u.get("device_uid"),
			"has_face": None if u.get("has_face") is None else cint(u.get("has_face")),
			"fingerprints": None if u.get("fingerprints") is None else cint(u.get("fingerprints")),
			"has_photo": None if u.get("has_photo") is None else cint(u.get("has_photo")),
		}
		values = {k: v for k, v in values.items() if v is not None or k in ("zk_user", "card_number")}
		row = existing.get(uid)
		if row:
			changed = {k: v for k, v in values.items() if row.get(k) != v}
			changed["last_seen"] = now
			frappe.db.set_value("ZK Device User", row.name, changed, update_modified=bool(len(changed) > 1))
			updated += len(changed) > 1
		else:
			frappe.get_doc(
				{"doctype": "ZK Device User", "device": device, "user_id": uid, "last_seen": now, **values}
			).insert(ignore_permissions=True)
			inserted += 1

	gone = set(removed)
	if cint(complete):
		gone |= set(existing) - seen
	if gone:
		frappe.db.delete("ZK Device User", {"device": device, "user_id": ("in", list(gone))})

	_refresh_user_summaries(None if cint(complete) else (seen | gone))
	frappe.db.commit()
	return {
		"inserted": inserted,
		"updated": updated,
		"removed": len(gone),
		"zk_users_created": created,
		"problems": problems[:50],
	}


def _create_zk_user(uid, u, employee_by_device_id, linked_employees):
	"""A person found on a device whom ERPNext did not know yet. Linked to the
	Employee who punches in under that ID, when there is exactly that one."""
	employee = employee_by_device_id.get(uid)
	if employee in linked_employees:
		employee = None
	name = (u.get("user_name") or "").strip() or uid
	if employee:
		name = frappe.db.get_value("Employee", employee, "employee_name") or name
	frappe.get_doc(
		{
			"doctype": "ZK User",
			"user_id": uid,
			"user_name": name,
			"employee": employee,
			"privilege": "Admin" if cint(u.get("privilege")) == ADMIN_PRIVILEGE else "User",
			"card_number": str(u.get("card_number") or "") or None,
		}
	).insert(ignore_permissions=True)
	if employee:
		linked_employees.add(employee)


def _refresh_user_summaries(user_ids=None):
	"""Recounts each ZK User's devices from the per-device rows."""
	condition, values = "", {}
	if user_ids is not None:
		if not user_ids:
			return
		condition = "where u.name in %(ids)s"
		values["ids"] = tuple(user_ids)
	frappe.db.sql(
		f"""update `tabZK User` u
		left join (
			select zk_user,
				count(*) as devices_on,
				sum(has_face) as with_face,
				sum(fingerprints > 0) as with_fp,
				sum(has_photo) as with_photo,
				max(last_seen) as last_seen
			from `tabZK Device User` where zk_user is not null group by zk_user
		) d on d.zk_user = u.name
		set u.devices_on = coalesce(d.devices_on, 0),
			u.devices_with_face = coalesce(d.with_face, 0),
			u.devices_with_fingerprint = coalesce(d.with_fp, 0),
			u.devices_with_photo = coalesce(d.with_photo, 0),
			u.last_reported = d.last_seen
		{condition}""",
		values,
	)
