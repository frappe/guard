frappe.ui.form.on("Custom Context", {
	refresh(frm) {
		if (frm.doc.body && ["Rule", "Pattern"].includes(frm.doc.context_type)) {
			frm.add_custom_button(__("Improve with AI"), async () => {
				const text = await frappe.xcall("guard.api.ui.improve_rule", { text: frm.doc.body });
				frappe.confirm(`${__("Use this rule?")}<br><br><b>${frappe.utils.escape_html(text)}</b>`, () => frm.set_value("body", text));
			});
		}
		if (frm.doc.status === "Suggested") {
			frm.add_custom_button(__("Approve"), () => frappe.xcall("guard.api.ui.set_context_status", { context: frm.doc.name, status: "Active" }).then(() => frm.reload_doc())).addClass("btn-primary");
			frm.add_custom_button(__("Ignore"), () => frappe.xcall("guard.api.ui.set_context_status", { context: frm.doc.name, status: "Ignored" }).then(() => frm.reload_doc()));
		}
	},
});
