// "Device User" on the Employee form: open the person's ZK User, or make one
// and put them on devices.

frappe.ui.form.on("Employee", {
	refresh(frm) {
		if (frm.is_new() || !frappe.model.can_read("ZK User")) return;
		frm.add_custom_button(
			__("Device User"),
			async () => {
				const existing = await frappe.db.get_value("ZK User", { employee: frm.doc.name }, "name");
				const name = existing.message && existing.message.name;
				if (name) return frappe.set_route("Form", "ZK User", name);
				if (!frappe.model.can_create("ZK User")) {
					return frappe.msgprint(__("{0} has no device user yet.", [frm.doc.employee_name]));
				}
				zk_create_device_user(frm);
			},
			__("Actions")
		);
	},
});

async function zk_create_device_user(frm) {
	const suggested = await frappe.call("zk_device_manager.api.suggest_user_id", { employee: frm.doc.name });
	const devices = await frappe.db.get_list("ZK Device", {
		filters: { enabled: 1 },
		fields: ["name", "ip_address"],
		order_by: "device_name asc",
		limit: 0,
	});
	const d = new frappe.ui.Dialog({
		title: __("Device user for {0}", [frm.doc.employee_name]),
		fields: [
			{
				fieldname: "user_id",
				fieldtype: "Data",
				label: __("User ID"),
				reqd: 1,
				default: suggested.message,
				description: frm.doc.attendance_device_id
					? __("Their Attendance Device ID.")
					: __("Becomes their Attendance Device ID."),
				read_only: frm.doc.attendance_device_id ? 1 : 0,
			},
			{ fieldname: "privilege", fieldtype: "Select", label: __("Privilege"), options: "User\nAdmin", default: "User" },
			{
				fieldname: "devices",
				fieldtype: "MultiCheck",
				label: __("Put on devices"),
				columns: 1,
				options: devices.map((x) => ({ label: `${x.name} (${x.ip_address})`, value: x.name })),
				description: __(
					"Their face and fingerprints still have to be enrolled on one device; they can then be copied to the rest from the device user."
				),
			},
		],
		primary_action_label: __("Create"),
		primary_action(v) {
			d.hide();
			frappe
				.call("zk_device_manager.api.create_user_from_employee", {
					employee: frm.doc.name,
					user_id: v.user_id,
					privilege: v.privilege,
					devices: v.devices || [],
				})
				.then((r) => {
					frm.reload_doc();
					frappe.set_route("Form", "ZK User", r.message.zk_user);
				});
		},
	});
	d.show();
}
