frappe.ui.form.on("AI Provider", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Fetch models"), async () => {
			const models = await frm.call("fetch_models").then((r) => r.message || []);
			frappe.msgprint(
				models.length
					? `<ul>${models
							.map((m) => `<li><code>${frappe.utils.escape_html(m)}</code></li>`)
							.join("")}</ul>`
					: __("The provider lists no models."),
				__("Models")
			);
		});
	},
});
