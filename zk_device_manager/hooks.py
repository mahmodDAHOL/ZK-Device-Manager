app_name = "zk_device_manager"
app_title = "ZK Device Manager"
app_publisher = "erpnext_mobile"
app_description = (
	"Manage the users, faces, fingerprints and photos on ZKTeco attendance "
	"devices from ERPNext, through an agent on the devices' own network."
)
app_email = "support@example.com"
app_license = "mit"

# The two roles the app's permissions are written against: HR, who ask for
# things to be done, and the agent's own account, which does them.
after_install = "zk_device_manager.install.after_install"
after_migrate = "zk_device_manager.install.after_install"

# "Create device user" on the Employee form.
doctype_js = {"Employee": "public/js/employee.js"}

scheduler_events = {
	"cron": {
		# Queues the scheduled full sync when it is due, and fails jobs an
		# agent claimed and never finished. Every five minutes is only how
		# often it looks: the sync itself runs at ZK Settings' interval.
		"*/5 * * * *": ["zk_device_manager.tasks.every_five_minutes"],
	}
}
