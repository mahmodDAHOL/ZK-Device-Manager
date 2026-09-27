import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime


def every_five_minutes():
	fail_stuck_jobs()
	queue_scheduled_sync()


def fail_stuck_jobs():
	"""A job the agent took and never closed — the PC was switched off, the
	agent crashed — would otherwise say Running for ever and hide that it
	never happened."""
	minutes = max(cint(frappe.db.get_single_value("ZK Settings", "stuck_job_minutes")) or 240, 60)
	cutoff = add_to_date(now_datetime(), minutes=-minutes)
	for name in frappe.get_all(
		"ZK Job",
		filters={"status": "Running", "started_at": ("<", cutoff)},
		pluck="name",
	):
		frappe.db.set_value(
			"ZK Job",
			name,
			{
				"status": "Failed",
				"finished_at": now_datetime(),
				"summary": _("The agent took this job and never reported back. Run it again."),
			},
		)
	frappe.db.commit()


def queue_scheduled_sync():
	"""Queues a full sync when one is due. The agent does the work; this only
	puts it in the queue, so a scheduled sync shows up as a job like any other."""
	settings = frappe.get_single("ZK Settings")
	if not cint(settings.auto_sync):
		return
	interval = max(cint(settings.sync_interval_minutes) or 120, 60)
	last = settings.last_scheduled_sync
	if last and get_datetime(last) > add_to_date(now_datetime(), minutes=-interval):
		return
	# One at a time: a sync still waiting or running is the one that is due.
	if frappe.db.exists("ZK Job", {"job_type": "Sync All Devices", "status": ("in", ("Queued", "Running"))}):
		return
	job = frappe.get_doc(
		{
			"doctype": "ZK Job",
			"job_type": "Sync All Devices",
			"copy_faces": cint(settings.sync_copy_faces),
			"copy_photos": cint(settings.sync_copy_photos),
		}
	)
	job.flags.ignore_permissions = True
	job.insert()
	frappe.db.set_single_value("ZK Settings", "last_scheduled_sync", now_datetime())
	frappe.db.commit()
