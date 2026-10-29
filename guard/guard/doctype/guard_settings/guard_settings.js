frappe.ui.form.on("Guard Settings", {
	setup(frm) {
		const enabled = () => ({ filters: { enabled: 1 } });
		["default", "base", "plus", "apex", "fast", "embedding"].forEach((slot) =>
			frm.set_query(`${slot}_ai_provider`, enabled)
		);
	},
	refresh(frm) {
		frm.add_custom_button(__("Fetch models"), async () => {
			// Model ids for each model field, from the AI provider of the field.
			const models = await frm.call("fetch_models").then((r) => r.message || {});
			Object.entries(models).forEach(([slot, ids]) =>
				frm.set_df_property(`${slot}_model`, "options", ids)
			);
			const providers = {};
			Object.entries(models).forEach(([slot, ids]) => {
				const name = frm.doc[`${slot}_ai_provider`] || frm.doc.default_ai_provider;
				if (name) providers[name] = ids;
			});
			const list = (ids) =>
				ids.length
					? `<ul>${ids
							.map((m) => `<li><code>${frappe.utils.escape_html(m)}</code></li>`)
							.join("")}</ul>`
					: `<p>${__("The AI provider lists no models.")}</p>`;
			frappe.msgprint(
				Object.entries(providers)
					.map(([name, ids]) => `<h5>${frappe.utils.escape_html(name)}</h5>${list(ids)}`)
					.join(""),
				__("Models")
			);
		});
	},
});
