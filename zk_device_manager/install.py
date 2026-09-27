import frappe

# HR: sees every device and person, and asks for users to be added, changed,
# removed, and copied between devices.
MANAGER_ROLE = "ZK Manager"

# The agent's account. Given to one API user only — the one whose key the
# office PC holds — and never to a person: it can report anything as true.
AGENT_ROLE = "ZK Agent"


def after_install():
	for role in (MANAGER_ROLE, AGENT_ROLE):
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1}
			).insert(ignore_permissions=True)
