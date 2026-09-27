import re

import frappe
from frappe import _
from frappe.model.document import Document

# pyzk writes a user ID of up to 24 bytes; the devices here use plain numbers,
# and a letter in one is refused by most firmware, so only digits are let in.
USER_ID = re.compile(r"^\d{1,24}$")


class ZKUser(Document):
	def validate(self):
		self.user_id = (self.user_id or "").strip()
		if not USER_ID.match(self.user_id):
			frappe.throw(_("User ID must be digits only, as it is typed on the device."))
		self.user_name = (self.user_name or "").strip() or self.user_id
		self._check_employee()

	def _check_employee(self):
		"""The Employee's Attendance Device ID is what turns a punch on the
		device into that Employee's check-in, so it must be this user's ID.
		Filled in when the Employee has none; refused when it names someone else."""
		if not self.employee:
			return
		current = frappe.db.get_value("Employee", self.employee, "attendance_device_id")
		current = (current or "").strip()
		if current and current != self.user_id:
			frappe.throw(
				_("Employee {0} punches in as {1}, not {2}. Change their Attendance Device ID first.").format(
					self.employee, current, self.user_id
				)
			)
		other = frappe.db.get_value(
			"ZK User", {"employee": self.employee, "name": ("!=", self.name)}, "name"
		)
		if other:
			frappe.throw(_("Employee {0} is already device user {1}.").format(self.employee, other))
		if not current:
			frappe.db.set_value("Employee", self.employee, "attendance_device_id", self.user_id)
