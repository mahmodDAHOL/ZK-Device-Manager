"""What the desk calls: asking for work to be done on the devices.

Nothing here reaches a device. Each call queues a ZK Job, which the agent on
the office network picks up (agent_api.claim_job), so the answer to "add this
person to the gate" is a job number, and the job's status says when it is done.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

JOB_TYPES = (
	"Sync All Devices",
	"Refresh Device State",
	"Add User",
	"Update User",
	"Remove User",
	"Copy Biometrics",
)


def _as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			value = [v for v in value.split(",")]
	return [str(v).strip() for v in value if str(v).strip()]


@frappe.whitelist()
def queue_job(
	job_type,
	zk_user=None,
	devices=None,
	source_device=None,
	copy_faces=1,
	copy_photos=1,
	copy_fingerprints=1,
	dry_run=0,
):
	"""Queues one job and answers its name."""
	if job_type not in JOB_TYPES:
		frappe.throw(_("Unknown job: {0}").format(job_type))
	frappe.has_permission("ZK Job", "create", throw=True)
	job = frappe.get_doc(
		{
			"doctype": "ZK Job",
			"job_type": job_type,
			"zk_user": zk_user or None,
			"source_device": source_device or None,
			"copy_faces": int(copy_faces or 0),
			"copy_photos": int(copy_photos or 0),
			"copy_fingerprints": int(copy_fingerprints or 0),
			"dry_run": int(dry_run or 0),
			"target_devices": [{"device": d} for d in _as_list(devices)],
		}
	)
	job.insert()
	return job.name


@frappe.whitelist()
def cancel_job(job):
	doc = frappe.get_doc("ZK Job", job)
	doc.check_permission("write")
	if doc.status != "Queued":
		frappe.throw(_("Only a job that has not started can be cancelled."))
	doc.status = "Cancelled"
	doc.summary = _("Cancelled by {0}").format(frappe.session.user)
	doc.finished_at = now_datetime()
	doc.save()
	return doc.status


@frappe.whitelist()
def suggest_user_id(employee=None):
	"""The ID a new device user should have: the Employee's Attendance Device
	ID when they have one, otherwise one past the highest ID in use."""
	if employee:
		current = frappe.db.get_value("Employee", employee, "attendance_device_id")
		if current:
			return str(current).strip()
	highest = frappe.db.sql(
		"""select max(cast(user_id as unsigned)) from `tabZK User` where user_id regexp '^[0-9]+$'"""
	)[0][0]
	used = frappe.db.sql(
		"""select max(cast(attendance_device_id as unsigned)) from `tabEmployee`
		where attendance_device_id regexp '^[0-9]+$'"""
	)[0][0]
	return str(max(int(highest or 0), int(used or 0)) + 1)


@frappe.whitelist()
def create_user_from_employee(employee, user_id=None, devices=None, privilege="User"):
	"""Makes the ZK User for an Employee and, when devices are given, queues
	putting them on those devices. Answers {"zk_user", "job"}."""
	frappe.has_permission("ZK User", "create", throw=True)
	emp = frappe.get_doc("Employee", employee)
	existing = frappe.db.get_value("ZK User", {"employee": employee}, "name")
	if existing:
		frappe.throw(_("{0} is already device user {1}.").format(emp.employee_name, existing))
	user_id = (user_id or suggest_user_id(employee)).strip()
	if frappe.db.exists("ZK User", user_id):
		zk_user = frappe.get_doc("ZK User", user_id)
		if zk_user.employee and zk_user.employee != employee:
			frappe.throw(_("Device user {0} is already {1}.").format(user_id, zk_user.employee_name))
		zk_user.employee = employee
		zk_user.save()
	else:
		zk_user = frappe.get_doc(
			{
				"doctype": "ZK User",
				"user_id": user_id,
				"user_name": emp.employee_name,
				"employee": employee,
				"privilege": privilege or "User",
			}
		).insert()
	job = None
	if _as_list(devices):
		job = queue_job("Add User", zk_user=zk_user.name, devices=devices)
	return {"zk_user": zk_user.name, "job": job}


@frappe.whitelist()
def agent_status():
	"""Whether the agent on the office network is alive, for the banner on the
	device and user forms. It is alive when it has asked for work within three
	of its own polling intervals."""
	# Not the cached copy: the heartbeat is written straight to the database
	# every poll, and a cached settings doc would report the agent as long dead.
	settings = frappe.get_single("ZK Settings")
	last = settings.last_heartbeat
	poll = max(int(settings.poll_seconds or 30), 10)
	online = bool(last) and get_datetime(last) >= add_to_date(now_datetime(), seconds=-3 * poll - 30)
	queued = frappe.db.count("ZK Job", {"status": "Queued"})
	running = frappe.db.count("ZK Job", {"status": "Running"})
	return {
		"online": online,
		"last_heartbeat": last,
		"host": settings.agent_host,
		"sdk_available": settings.sdk_available,
		"queued": queued,
		"running": running,
	}
