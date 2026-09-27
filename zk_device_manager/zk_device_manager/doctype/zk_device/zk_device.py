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
