// A job's progress bar, and how long it has left.
//
// The agent reports every few seconds (agent_api.report_progress); the server
// works out the time left and pushes it to this form's room as
// "zk_job_progress", so the bar moves without the page reloading. The form
// also re-reads the job every 15 seconds, for sites where realtime updates do
// not reach the browser.

frappe.ui.form.on("ZK Job", {
	setup() {
		zk_listen_for_progress();
	},

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

		zk_draw_progress(frm, frm.doc);
		if (!frm.is_new() && ["Queued", "Running"].includes(frm.doc.status)) zk_show_estimate(frm);

		clearTimeout(frm._zk_poll);
		if (!frm.is_new() && ["Queued", "Running"].includes(frm.doc.status)) {
			frm._zk_poll = setTimeout(() => {
				if (!frm.is_dirty() && cur_frm === frm) frm.reload_doc();
			}, 15000);
		}
	},
});

function zk_listen_for_progress() {
	if (window.__zk_progress_listening) return;
	window.__zk_progress_listening = true;
	frappe.realtime.on("zk_job_progress", (data) => {
		const frm = cur_frm;
		if (!frm || frm.doctype !== "ZK Job" || frm.doc.name !== data.job) return;
		if (data.reload) {
			// Done or failed: the whole record changed, so read it again.
			if (!frm.is_dirty()) frm.reload_doc();
			return;
		}
		Object.assign(frm.doc, {
			progress: data.progress,
			progress_message: data.progress_message,
			remaining_seconds: data.remaining_seconds,
			estimated_finish: data.estimated_finish,
			progress_at: data.progress_at,
		});
		zk_draw_progress(frm, frm.doc);
	});
}

// The agent reports at least every few seconds while it works, so a running
// job silent for this long means the agent stopped: crashed, or its computer
// went off. Saying "less than a minute left" then would be a lie.
const ZK_SILENT_AFTER_SECONDS = 180;

function zk_seconds_since(datetime) {
	if (!datetime) return null;
	// Stored in the site's time zone; compared in the reader's.
	const user_tz = frappe.datetime.convert_to_user_tz ? frappe.datetime.convert_to_user_tz(datetime) : datetime;
	return moment().diff(moment(user_tz), "seconds");
}

function zk_draw_progress(frm, doc) {
	const title = __("Progress");
	if (doc.status !== "Running") {
		frm.dashboard.hide_progress && frm.dashboard.hide_progress(title);
		return;
	}
	const percent = Math.max(0, Math.min(100, doc.progress || 0));
	const silent = zk_seconds_since(doc.progress_at);
	const parts = [];
	if (doc.progress_message) parts.push(frappe.utils.escape_html(doc.progress_message));
	if (silent !== null && silent > ZK_SILENT_AFTER_SECONDS) {
		// Don't embed <span> — use the color argument instead
		frm.dashboard.set_headline_alert(
			parts.join(" · "),
			"red"      // ← this is what the red <span> was trying to do
		);
	} else {
		frm.dashboard.set_headline(parts.join(" · "));
	}
	frm.dashboard.show_progress(title, percent, `${percent.toFixed(0)}% · ${parts.join(" · ")}`);

	// Keep the silence check honest between reports, which do not come when
	// the agent is dead.
	clearTimeout(frm._zk_silence);
	frm._zk_silence = setTimeout(() => {
		if (cur_frm === frm && frm.doc.status === "Running") zk_draw_progress(frm, frm.doc);
	}, 30000);
}

function zk_time_left(seconds) {
	if (seconds === null || seconds === undefined || seconds === "") return __("calculating time left…");
	seconds = Math.round(seconds);
	if (seconds < 60) return __("less than a minute left");
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return __("about {0} min left", [minutes]);
	const hours = Math.floor(minutes / 60);
	return __("about {0} h {1} min left", [hours, minutes % 60]);
}

function zk_minutes(seconds) {
	const m = Math.max(1, Math.round(seconds / 60));
	return m < 60 ? __("{0} min", [m]) : __("{0} h {1} min", [Math.floor(m / 60), m % 60]);
}

function zk_show_estimate(frm) {
	frappe
		.call("zk_device_manager.api.job_estimate", { job_type: frm.doc.job_type, dry_run: frm.doc.dry_run })
		.then((r) => {
			const e = r.message || {};
			let text;
			if (!e.count) {
				text = __("No {0} has finished yet, so there is nothing to estimate from.", [__(frm.doc.job_type)]);
			} else if (e.count === 1 || Math.abs(e.max - e.min) < 60) {
				text = __("Last time this took about {0}.", [zk_minutes(e.median)]);
			} else {
				text = __("The last {0} took {1} to {2}.", [e.count, zk_minutes(e.min), zk_minutes(e.max)]);
			}
			if (frm.doc.status === "Queued") text = __("Waiting for the agent.") + " " + text;
			frm.dashboard.set_headline_alert(text, frm.doc.status === "Queued" ? "blue" : "orange");
		});
}
