import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document


class ZKDevice(Document):
	def validate(self):
		self.ip_address = (self.ip_address or "").strip()
		try:
			ipaddress.ip_address(self.ip_address)
		except ValueError:
			frappe.throw(_("{0} is not an IP address.").format(self.ip_address))
		if not self.port:
			self.port = 4370
		if not 1 <= int(self.port) <= 65535:
			frappe.throw(_("Port must be between 1 and 65535."))
		if self.attendance_slot:
			# Two devices in one slot would overwrite each other's attendance
			# file, and one of them would never reach Fetch Checkins.
			other = frappe.db.get_value(
				"ZK Device",
				{"attendance_slot": self.attendance_slot, "name": ("!=", self.name)},
				"name",
			)
			if other:
				frappe.throw(_("{0} already uses attendance slot {1}.").format(other, self.attendance_slot))
