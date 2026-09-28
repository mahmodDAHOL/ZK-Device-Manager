import frappe
from frappe import _
from frappe.model.document import Document

from zk_device_manager.install import AGENT_ROLE

# Jobs about one person, which cannot run without knowing who.
USER_JOBS = {"Add User", "Update User", "Remove User", "Copy Biometrics"}

# Jobs that write to a device by name, which must be told which ones. Update
# User may be left empty (every device they are on); these may not: adding or
# removing someone "everywhere" by leaving a box empty is too easy to do by
# accident.
EXPLICIT_TARGETS = {"Add User", "Remove User", "Copy Biometrics"}


class ZKJob(Document):
	def before_insert(self):
		self.requested_by = frappe.session.user
		self.status = "Queued"
		for field in (
			"claimed_by", "started_at", "finished_at", "summary", "error", "result", "sync_log",
			"progress", "progress_message", "estimated_finish", "remaining_seconds", "progress_at",
		):
			self.set(field, None)

	def validate(self):
		if self.job_type in USER_JOBS and not self.zk_user:
			frappe.throw(_("{0} needs a user.").format(_(self.job_type)))
		if self.job_type in EXPLICIT_TARGETS and not self.target_devices:
			frappe.throw(_("Choose the devices for {0}.").format(_(self.job_type)))
		if self.source_device and self.source_device in self.target_device_names():
			frappe.throw(_("A device cannot be copied from and to at once."))
		if self.job_type == "Remove User":
			# Nothing to copy when taking someone off.
			self.copy_faces = self.copy_photos = self.copy_fingerprints = 0
		self._keep_finished_jobs_as_they_were()

	def target_device_names(self):
		return [row.device for row in (self.target_devices or [])]

	def _keep_finished_jobs_as_they_were(self):
		"""Once the agent has it, a job is a record of what was done. Only the
		agent writes to it after that — and a person may cancel one still queued."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or before.status == "Queued":
			return
		if AGENT_ROLE in frappe.get_roles() or "System Manager" in frappe.get_roles():
			return
		frappe.throw(_("A job the agent has started can no longer be changed."))
