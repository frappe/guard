frappe.ui.form.on("Code Review", {
	refresh(frm) {
		if (["Queued", "Running"].includes(frm.doc.status)) frm.add_custom_button(__("Cancel"), () => frappe.xcall("guard.api.ui.cancel_review", { review: frm.doc.name }).then(() => frm.reload_doc()));
		if (["Failed", "Cancelled", "Skipped"].includes(frm.doc.status) && frm.doc.pull_request) frm.add_custom_button(__("Retry"), () => frappe.xcall("guard.api.ui.retry_review", { review: frm.doc.name }).then((n) => frappe.set_route("Form", "Code Review", n)));
		if (frm.doc.pull_request) frm.add_custom_button(__("Open review"), () => frappe.set_route("guard-pr", frm.doc.pull_request));
		if (frm.doc.status === "Running") frm.dashboard.set_headline(__("Stage: {0}", [frm.doc.stage || ""]));
		frappe.realtime.on("guard_review_updated", (d) => d.review === frm.doc.name && frm.reload_doc());
	},
});
