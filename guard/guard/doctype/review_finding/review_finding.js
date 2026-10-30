frappe.ui.form.on("Review Finding", {
	async refresh(frm) {
		if (frm.doc.comment_url)
			frm.add_custom_button(__("GitHub"), () => window.open(frm.doc.comment_url));
		if (frm.doc.pull_request)
			frm.add_custom_button(__("Open review"), () =>
				frappe.set_route("guard-pr", frm.doc.pull_request)
			);
		const next = frm.doc.status === "Open" ? "Dismissed" : "Open";
		if (["Open", "Dismissed"].includes(frm.doc.status)) {
			frm.add_custom_button(next === "Open" ? __("Reopen") : __("Dismiss"), () =>
				optimistic(frm, { status: next }, () =>
					frappe.xcall("guard.api.ui.set_finding_status", {
						finding: frm.doc.name,
						status: next,
					})
				)
			);
		}
		const prompt = frm.doc.fix_prompt || "";
		frm.get_field("fix_html").$wrapper.html(`<div class="frappe-card" style="padding:12px">
			<b>${__("Fix with your agent")}</b>
			<div style="margin-top:8px">
				<button class="btn btn-xs btn-default copy">${__("Copy prompt")}</button>
				<a class="btn btn-xs btn-default" href="cursor://anysphere.cursor-deeplink/prompt?text=${encodeURIComponent(
					prompt.slice(0, 6000)
				)}">Cursor</a>
				<a class="btn btn-xs btn-default copy" data-open="https://app.conductor.build">Conductor</a>
				<a class="btn btn-xs btn-default copy" data-open="https://app.devin.ai">Devin</a>
			</div>
			<p class="text-muted small" style="margin-top:8px">${__(
				"For Claude Code, Codex and other agents, copy the prompt and paste it."
			)}</p></div>`);
		frm.get_field("fix_html")
			.$wrapper.find(".copy")
			.on("click", (e) => {
				frappe.utils.copy_to_clipboard(prompt);
				const open = $(e.currentTarget).data("open");
				if (open) window.open(open);
			});
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
