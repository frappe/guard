frappe.ui.form.on("Review Settings", {
	refresh(frm) {
		if (frm.doc.scope === "Team" && !frm.is_new()) {
			frm.add_custom_button(__("Sync from organization"), () =>
				frm.call("sync_from_organization").then(() => frm.reload_doc())
			);
		}
	},
});
