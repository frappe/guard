frappe.ui.form.on("Custom Context", {
	refresh(frm) {
		if (frm.doc.body && ["Rule", "Pattern"].includes(frm.doc.context_type)) {
			frm.add_custom_button(__("Improve with AI"), async () => {
				const text = await frappe.xcall("guard.api.ui.improve_rule", {
					text: frm.doc.body,
				});
				frappe.confirm(
					`${__("Use this rule?")}<br><br><b>${frappe.utils.escape_html(text)}</b>`,
					() => frm.set_value("body", text)
				);
			});
		}
		if (frm.doc.status === "Suggested") {
			frm.add_custom_button(__("Approve"), () => set_status(frm, "Active")).addClass(
				"btn-primary"
			);
			frm.add_custom_button(__("Ignore"), () => set_status(frm, "Ignored"));
		}
		if (frm.doc.status === "Active" && !frm.is_new()) {
			frm.add_custom_button(__("Retire"), () =>
				frappe.confirm(__("Stop using this in reviews?"), () =>
					set_status(frm, "Inactive")
				)
			);
		}
		if (frm.doc.status === "Inactive") {
			frm.add_custom_button(__("Restore"), () => set_status(frm, "Active"));
		}
		render_evidence(frm);
	},
});

function set_status(frm, status) {
	return frappe
		.xcall("guard.api.ui.set_context_status", { context: frm.doc.name, status })
		.then(() => frm.reload_doc());
}

async function render_evidence(frm) {
	const wrapper = frm.get_field("evidence_html").$wrapper;
	wrapper.empty();
	frm.toggle_display("evidence_section", false);
	if (frm.is_new() || !frm.doc.evidence) return;
	const entries = await frappe.xcall("guard.api.ui.get_context_evidence", {
		context: frm.doc.name,
	});
	frm.toggle_display("evidence_section", entries.length > 0);
	const rows = entries.map((entry) => {
		const escape = (value) => frappe.utils.escape_html(value || "");
		if (entry.missing) {
			return `<tr><td colspan="3" class="text-muted">${escape(entry.name)}: ${__(
				"deleted"
			)}</td></tr>`;
		}
		const kind = [entry.kind, entry.sentiment].filter(Boolean).join(" · ");
		const pull_request = entry.pull_request_url
			? `<a href="${escape(entry.pull_request_url)}" target="_blank">#${
					entry.pull_request_number
			  } ${escape(entry.pull_request_title)}</a>`
			: escape(entry.pull_request);
		const finding = entry.finding_title
			? `<div class="text-muted small">${__("On finding")}: ${escape(
					entry.finding_title
			  )}</div>`
			: "";
		return `<tr>
			<td>${escape(entry.author) || __("Guard")}<div class="text-muted small">${escape(kind)}</div></td>
			<td>${escape(entry.body)}${finding}</td>
			<td>${pull_request}</td>
		</tr>`;
	});
	wrapper.html(`<table class="table table-bordered">
		<thead><tr><th>${__("Who")}</th><th>${__("Said")}</th><th>${__("Pull Request")}</th></tr></thead>
		<tbody>${rows.join("")}</tbody>
	</table>`);
}
