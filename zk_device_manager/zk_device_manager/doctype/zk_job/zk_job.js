frappe.ui.form.on("ZK Job", {
	refresh(frm) {
		const colour = { Queued: "blue", Running: "orange", Done: "green", Failed: "red", Cancelled: "gray" }[
			frm.doc.status
		];
		if (!frm.is_new()) frm.page.set_indicator(__(frm.doc.status), colour);

		if (!frm.is_new() && frm.doc.status === "Queued") {
			frm.add_custom_button(__("Cancel Job"), () =>
				frappe.call("zk_device_manager.api.cancel_job", { job: frm.doc.name }).then(() => frm.reload_doc())
			);
		}
		if (frm.doc.sync_log) {
			frm.add_custom_button(__("Open Sync Log"), () => frappe.set_route("Form", "ZK Sync Log", frm.doc.sync_log));
		}

		// Watch it finish: the agent writes the result, the form shows it.
		clearTimeout(frm._zk_poll);
		if (!frm.is_new() && ["Queued", "Running"].includes(frm.doc.status)) {
			frm._zk_poll = setTimeout(() => {
				if (!frm.is_dirty() && cur_frm === frm) frm.reload_doc();
			}, 10000);
		}
	},
});
