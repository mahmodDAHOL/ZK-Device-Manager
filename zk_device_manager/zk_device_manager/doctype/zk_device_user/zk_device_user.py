from frappe.model.document import Document


class ZKDeviceUser(Document):
	# Written by the agent only (see agent_api.report_users), and in bulk, so
	# nothing is checked here that would have to be checked 478 times a device.
	pass
