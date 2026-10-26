frappe.ui.form.on("Code Repository", {
	setup(frm) {
		for (const event of ["guard_index_updated", "guard_repository_updated"])
			frappe.realtime.on(
				event,
				(data) => data.repository === frm.doc.name && frm.reload_doc()
			);
	},
	refresh(frm) {
		const index = (full) =>
			optimistic(frm, { index_status: "Queued" }, () =>
				frappe.xcall("guard.api.ui.index_now", { repository: frm.doc.name, full })
			);
		frm.add_custom_button(__("Index now"), () => index(0), __("Index"));
		frm.add_custom_button(__("Full rebuild"), () => index(1), __("Index"));
		frm.add_custom_button(__("Build knowledge base"), () =>
			optimistic(frm, { kb_status: "Queued" }, () =>
				frappe.xcall("guard.api.ui.build_knowledge_base", { repository: frm.doc.name })
			)
		);
		frm.add_custom_button(__("Explain config"), () => {
			frappe.prompt(
				{
					fieldname: "path",
					fieldtype: "Data",
					label: __("File path (blank for the root)"),
				},
				async (v) => {
					const result = await frappe.xcall("guard.api.ui.explain_config", {
						repository: frm.doc.name,
						path: v.path || "",
					});
					const rows = Object.entries(result.settings)
						.map(
							([k, item]) =>
								`<tr><td>${k}</td><td><code>${frappe.utils.escape_html(
									JSON.stringify(item.value)
								)}</code></td><td>${frappe.utils.escape_html(
									item.source
								)}</td></tr>`
						)
						.join("");
					const rules = result.rules
						.map(
							(r) =>
								`<li>${frappe.utils.escape_html(
									r.rule
								)} <span class="text-muted">(${frappe.utils.escape_html(
									r.source
								)})</span></li>`
						)
						.join("");
					const errors = result.errors
						.map((e) => `<li class="text-danger">${frappe.utils.escape_html(e)}</li>`)
						.join("");
					frappe.msgprint(
						`<table class="table table-sm">${rows}</table><h5>${__(
							"Rules"
						)}</h5><ul>${rules}</ul>${
							errors ? `<h5>${__("Problems")}</h5><ul>${errors}</ul>` : ""
						}`,
						__("Effective config")
					);
				}
			);
		});
		frm.add_custom_button(__("Codebase chat"), () => frappe.set_route("guard-chat"));
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
