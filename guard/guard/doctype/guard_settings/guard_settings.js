frappe.ui.form.on("Guard Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Fetch models"), async () => {
			const models = await frm.call("fetch_models").then((r) => r.message || []);
			const fields = ["base_model", "plus_model", "apex_model", "fast_model", "embedding_model"];
			fields.forEach((f) => frm.set_df_property(f, "options", models));
			frappe.msgprint(models.length ? `<ul>${models.map((m) => `<li><code>${frappe.utils.escape_html(m)}</code></li>`).join("")}</ul>` : __("The gateway lists no models."), __("Models"));
		});
	},
});
