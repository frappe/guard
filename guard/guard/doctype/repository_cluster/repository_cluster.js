frappe.ui.form.on("Repository Cluster", {
	refresh(frm) {
		if (frm.doc.status !== "Suggested") return;
		frm.add_custom_button(__("Use this"), () => frappe.xcall("guard.api.ui.set_cluster_status", { cluster: frm.doc.name, status: "Active" }).then(() => frm.reload_doc())).addClass("btn-primary");
		frm.add_custom_button(__("Discard"), () => frappe.xcall("guard.api.ui.set_cluster_status", { cluster: frm.doc.name, status: "Discarded" }).then(() => frm.reload_doc()));
	},
});
