import frappe
from frappe import _
from frappe.model.document import Document


class ZKSettings(Document):
	def validate(self):
		# A full sync reads every user on every device one at a time and takes
		# the better part of an hour; queuing them faster than that only stacks
		# them up behind each other.
		if self.auto_sync and (self.sync_interval_minutes or 0) < 60:
			frappe.throw(_("The scheduled sync cannot run more often than every 60 minutes."))
		if self.auto_fetch_attendance:
			if (self.attendance_interval_minutes or 0) < 10:
				frappe.throw(_("Attendance cannot be fetched more often than every 10 minutes."))
			if not (self.attendance_company and self.attendance_docname):
				frappe.throw(_("Set the Company and the record to upload attendance to."))
		if (self.poll_seconds or 0) < 10:
			self.poll_seconds = 10
		if (self.stuck_job_minutes or 0) < 60:
			self.stuck_job_minutes = 60
