frappe.ui.form.on("ZK Device", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Refresh State"), () =>
			frappe
				.call("zk_device_manager.api.queue_job", {
					job_type: "Refresh Device State",
					devices: [frm.doc.name],
				})
				.then((r) => frappe.show_alert({ message: __("Queued as {0}", [r.message]), indicator: "green" }))
		);
		frm.add_custom_button(__("Sync All Devices"), () =>
			frappe.confirm(
				__("Copy every missing user, face and photo between all enabled devices? This takes a while."),
				() =>
					frappe
						.call("zk_device_manager.api.queue_job", { job_type: "Sync All Devices" })
						.then((r) =>
							frappe.show_alert({ message: __("Queued as {0}", [r.message]), indicator: "green" })
						)
			)
		);
		frm.add_custom_button(__("Fetch Attendance"), () =>
			frappe
				.call("zk_device_manager.api.queue_job", { job_type: "Fetch Attendance" })
				.then((r) => frappe.show_alert({ message: __("Queued as {0}", [r.message]), indicator: "green" }))
		);
		frm.add_custom_button(__("Users on Device"), () =>
			frappe.set_route("List", "ZK Device User", { device: frm.doc.name })
		);
		const colour = { Online: "green", Offline: "red" }[frm.doc.status] || "gray";
		frm.page.set_indicator(__(frm.doc.status || "Unknown"), colour);
	},
});
