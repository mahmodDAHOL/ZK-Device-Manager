// Asking for this person to be put on, changed on, copied to, or taken off
// devices. Every button queues a ZK Job; the agent on the office network does
// the work and the job says when it is done.

frappe.ui.form.on("ZK User", {
	refresh(frm) {
		if (frm.is_new()) return;
		zk_show_agent_status(frm);

		const group = __("Devices");
		frm.add_custom_button(__("Add to Devices"), () => zk_user_add(frm), group);
		frm.add_custom_button(__("Copy Face / Photo"), () => zk_user_copy(frm), group);
		frm.add_custom_button(__("Update on Devices"), () => zk_user_update(frm), group);
		frm.add_custom_button(__("Remove from Devices"), () => zk_user_remove(frm), group);
		frm.add_custom_button(__("Jobs"), () =>
			frappe.set_route("List", "ZK Job", { zk_user: frm.doc.name })
		);
	},
});

async function zk_devices_on(user) {
	return (await frappe.db.get_list("ZK Device User", {
		filters: { zk_user: user },
		fields: ["device"],
		limit: 0,
	})).map((r) => r.device);
}

async function zk_device_options(checked = [], exclude = []) {
	const devices = await frappe.db.get_list("ZK Device", {
		filters: { enabled: 1 },
		fields: ["name", "ip_address"],
		order_by: "device_name asc",
		limit: 0,
	});
	return devices
		.filter((d) => !exclude.includes(d.name))
		.map((d) => ({
			label: `${d.name} (${d.ip_address})`,
			value: d.name,
			checked: checked.includes(d.name),
		}));
}

function zk_queue(args, done_message) {
	return frappe
		.call("zk_device_manager.api.queue_job", args)
		.then((r) => {
			frappe.show_alert({ message: done_message || __("Queued as {0}", [r.message]), indicator: "green" });
			return r.message;
		});
}

async function zk_user_add(frm) {
	const on = await zk_devices_on(frm.doc.name);
	const options = await zk_device_options([], on);
	if (!options.length) {
		frappe.msgprint(__("{0} is already on every enabled device.", [frm.doc.user_name]));
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Add {0} to devices", [frm.doc.user_name]),
		fields: [
			{ fieldname: "devices", fieldtype: "MultiCheck", label: __("Devices"), options, columns: 1, reqd: 1 },
			{ fieldtype: "Section Break", label: __("Copy from the devices that have them") },
			{ fieldname: "copy_faces", fieldtype: "Check", label: __("Face"), default: 1 },
			{ fieldname: "copy_photos", fieldtype: "Check", label: __("Photo"), default: 1 },
			{ fieldname: "copy_fingerprints", fieldtype: "Check", label: __("Fingerprints"), default: 1 },
		],
		primary_action_label: __("Add"),
		primary_action(v) {
			if (!v.devices || !v.devices.length) return frappe.msgprint(__("Choose at least one device."));
			d.hide();
			zk_queue({
				job_type: "Add User",
				zk_user: frm.doc.name,
				devices: v.devices,
				copy_faces: v.copy_faces,
				copy_photos: v.copy_photos,
				copy_fingerprints: v.copy_fingerprints,
			});
		},
	});
	d.show();
}

async function zk_user_copy(frm) {
	const on = await zk_devices_on(frm.doc.name);
	const d = new frappe.ui.Dialog({
		title: __("Copy {0}'s face and photo", [frm.doc.user_name]),
		fields: [
			{
				fieldname: "source_device",
				fieldtype: "Link",
				options: "ZK Device",
				label: __("Copy From"),
				description: __("Leave empty to take them from any device that has them."),
			},
			{
				fieldname: "devices",
				fieldtype: "MultiCheck",
				label: __("Copy To"),
				options: await zk_device_options(on),
				columns: 1,
				description: __("Only what a device is missing is copied; nothing it has is replaced."),
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "copy_faces", fieldtype: "Check", label: __("Face"), default: 1 },
			{ fieldname: "copy_photos", fieldtype: "Check", label: __("Photo"), default: 1 },
			{ fieldname: "copy_fingerprints", fieldtype: "Check", label: __("Fingerprints"), default: 1 },
		],
		primary_action_label: __("Copy"),
		primary_action(v) {
			const devices = (v.devices || []).filter((x) => x !== v.source_device);
			if (!devices.length) return frappe.msgprint(__("Choose at least one device to copy to."));
			d.hide();
			zk_queue({
				job_type: "Copy Biometrics",
				zk_user: frm.doc.name,
				devices,
				source_device: v.source_device,
				copy_faces: v.copy_faces,
				copy_photos: v.copy_photos,
				copy_fingerprints: v.copy_fingerprints,
			});
		},
	});
	d.show();
}

function zk_user_update(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save first: the devices are given what is saved."));
		return;
	}
	frappe.confirm(
		__("Write {0}'s name, privilege and card to every device they are on?", [frm.doc.user_name]),
		() => zk_queue({ job_type: "Update User", zk_user: frm.doc.name })
	);
}

async function zk_user_remove(frm) {
	const on = await zk_devices_on(frm.doc.name);
	if (!on.length) {
		frappe.msgprint(__("{0} is on no device.", [frm.doc.user_name]));
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Remove {0} from devices", [frm.doc.user_name]),
		fields: [
			{
				fieldname: "devices",
				fieldtype: "MultiCheck",
				label: __("Devices"),
				options: (await zk_device_options(on)).filter((o) => on.includes(o.value)),
				columns: 1,
			},
			{
				fieldtype: "HTML",
				options: `<p class="text-danger">${__(
					"They will no longer be able to punch in on these devices. Their face is saved on the agent's computer first."
				)}</p>`,
			},
		],
		primary_action_label: __("Remove"),
		primary_action(v) {
			if (!v.devices || !v.devices.length) return frappe.msgprint(__("Choose at least one device."));
			d.hide();
			frappe.confirm(
				__("Remove {0} from {1} device(s)?", [frm.doc.user_name, v.devices.length]),
				() => zk_queue({ job_type: "Remove User", zk_user: frm.doc.name, devices: v.devices })
			);
		},
	});
	d.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
	d.show();
}

function zk_show_agent_status(frm) {
	frappe.call("zk_device_manager.api.agent_status").then((r) => {
		const s = r.message || {};
		if (s.online) {
			const busy = s.queued + s.running;
			frm.dashboard.set_headline_alert(
				busy
					? __("Agent online. {0} job(s) waiting or running.", [busy])
					: __("Agent online."),
				"green"
			);
		} else {
			frm.dashboard.set_headline_alert(
				__("The agent on the office network is not running, so queued jobs will wait. Last seen: {0}", [
					s.last_heartbeat ? frappe.datetime.comment_when(s.last_heartbeat) : __("never"),
				]),
				"orange"
			);
		}
	});
}
