frappe.ui.form.on("Repository Cluster", {
	refresh(frm) {
		if (frm.doc.status !== "Suggested") return;
		frm.add_custom_button(__("Use this"), () => set_status(frm, "Active")).addClass(
			"btn-primary"
		);
		frm.add_custom_button(__("Discard"), () => set_status(frm, "Discarded"));
	},
});

// Show the new status at once. On an error, load the saved record again.
function set_status(frm, status) {
	Object.assign(frm.doc, { status });
	frm.refresh();
	return frappe.xcall("guard.api.ui.set_cluster_status", { cluster: frm.doc.name, status }).then(
		() => frm.reload_doc(),
		() => frm.reload_doc()
	);
}
