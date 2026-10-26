frappe.ui.form.on("Code Review", {
	setup(frm) {
		frappe.realtime.on(
			"guard_review_updated",
			(d) => d.review === frm.doc.name && frm.reload_doc()
		);
	},
	refresh(frm) {
		if (["Queued", "Running"].includes(frm.doc.status))
			frm.add_custom_button(__("Cancel"), () =>
				optimistic(
					frm,
					{ status: "Cancelled", cancelled_by: frappe.session.user, stage: "" },
					() => frappe.xcall("guard.api.ui.cancel_review", { review: frm.doc.name })
				)
			);
		if (["Failed", "Cancelled", "Skipped"].includes(frm.doc.status) && frm.doc.pull_request)
			frm.add_custom_button(__("Retry"), () => {
				frappe.show_alert({ message: __("Queuing a new review"), indicator: "blue" });
				frappe
					.xcall("guard.api.ui.retry_review", { review: frm.doc.name })
					.then((n) => n && frappe.set_route("Form", "Code Review", n));
			});
		if (frm.doc.pull_request)
			frm.add_custom_button(__("Open review"), () =>
				frappe.set_route("guard-pr", frm.doc.pull_request)
			);
		if (frm.doc.status === "Running")
			frm.dashboard.set_headline(__("Stage: {0}", [frm.doc.stage || ""]));
	},
});

// Show `values` on the form at once, then make the call. Then load the saved record, also on an error.
function optimistic(frm, values, call) {
	Object.assign(frm.doc, values);
	frm.refresh();
	return call().then(
		() => frm.reload_doc(),
		() => frm.reload_doc()
	);
}
