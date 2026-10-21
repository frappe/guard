frappe.ui.form.on("Code Provider", {
	async refresh(frm) {
		if (frm.is_new()) return;
		frm.set_value(
			"webhook_url",
			await frappe.xcall("guard.api.ui.webhook_url", { provider: frm.doc.name })
		);
		frm.add_custom_button(__("Test connection"), async () => {
			const r = await frappe.xcall("guard.api.ui.test_provider", { provider: frm.doc.name });
			frappe.show_alert({ message: r.message, indicator: r.ok ? "green" : "red" }, 8);
		});
		frm.add_custom_button(__("Sync repositories"), async () => {
			const n = await frappe.xcall("guard.api.ui.sync_repositories", {
				provider: frm.doc.name,
			});
			frappe.show_alert(__("{0} repositories synced", [n]));
		});
	},
});
