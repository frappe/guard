frappe.ui.form.on("Pull Request", {
	refresh(frm) {
		frm.add_custom_button(__("Open review"), () =>
			frappe.set_route("guard-pr", frm.doc.name)
		).addClass("btn-primary");
		frm.add_custom_button(__("Re-review"), async () => {
			const name = await frappe.xcall("guard.api.ui.rereview", {
				pull_request: frm.doc.name,
			});
			frappe.show_alert({
				message: name ? __("Review {0} queued", [name]) : __("Review skipped"),
				indicator: name ? "green" : "orange",
			});
		});
		frm.add_custom_button(__("Fix all"), () =>
			frappe.set_route("guard-pr", frm.doc.name, { fix: "all" })
		);
		if (frm.doc.web_url)
			frm.add_custom_button(__("GitHub"), () => window.open(frm.doc.web_url));
		render_overview(frm);
	},
});

async function render_overview(frm) {
	const data = await frappe.xcall("guard.api.ui.pull_request_view", {
		pull_request: frm.doc.name,
	});
	const review = data.review;
	const rows = data.findings
		.map(
			(f) =>
				`<tr><td>${f.severity}</td><td><a href="/desk/review-finding/${
					f.name
				}">${frappe.utils.escape_html(f.title)}</a></td><td><code>${f.path}:${
					f.start_line
				}</code></td><td>${f.status}</td></tr>`
		)
		.join("");
	frm.get_field("overview_html").$wrapper.html(
		review
			? `<h4>${__("Confidence")}: ${review.confidence_score}/5 · ${__("Risk")}: ${
					review.risk_level || "–"
			  }</h4>${frappe.markdown(review.summary || "")}
			<table class="table table-sm" style="margin-top:12px"><tbody>${
				rows || `<tr><td>${__("No findings.")}</td></tr>`
			}</tbody></table>
			${review.diagram ? `<pre>${frappe.utils.escape_html(review.diagram)}</pre>` : ""}`
			: `<p class="text-muted">${__("No review yet.")}</p>`
	);
}
