// Codebase chat: ask questions about one or more indexed repositories.

frappe.pages["guard-chat"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Codebase Chat"),
		single_column: true,
	});
	const messages = [];
	const repos = page.add_field({
		fieldname: "repositories",
		fieldtype: "MultiSelectList",
		label: __("Repositories"),
		get_data: (txt) =>
			frappe.db.get_link_options("Code Repository", txt, { index_status: "Indexed" }),
	});
	const genius = page.add_field({
		fieldname: "genius",
		fieldtype: "Check",
		label: __("Deep answer"),
	});
	// Desk gives page.main no padding: line the content up with the page title and fields.
	const $body = $(
		`<div style="padding: var(--padding-sm) var(--padding-md) var(--padding-lg)"></div>`
	).appendTo(page.main);
	const $log = $(
		`<div style="max-height:65vh;overflow:auto;margin-bottom:12px"></div>`
	).appendTo($body);
	const $input = $(
		`<textarea class="form-control" rows="3" placeholder="${__(
			"Ask about the code. Ctrl+Enter to send."
		)}"></textarea>`
	).appendTo($body);
	const session = frappe.utils.get_random(12);

	const add = (role, html) =>
		$log
			.append(
				`<div style="margin-bottom:12px"><b>${
					role === "user" ? __("You") : "Guard"
				}</b><div>${html}</div></div>`
			)
			.scrollTop(1e9);
	const send = async () => {
		const text = $input.val().trim();
		const selected = repos.get_value() || [];
		if (!text) return;
		if (!selected.length) {
			frappe.msgprint(__("Pick at least one indexed repository."));
			return;
		}
		messages.push({ role: "user", content: text });
		add("user", frappe.utils.escape_html(text));
		$input.val("");
		const reply = await frappe.xcall("guard.api.v2.query", {
			messages,
			repositories: selected,
			session_id: session,
			genius: !!genius.get_value(),
		});
		messages.push({ role: "assistant", content: reply.message });
		const sources = (reply.sources || [])
			.map(
				(s) =>
					`<li><code>${frappe.utils.escape_html(
						s.repository
					)}:${frappe.utils.escape_html(s.filepath)}:${s.linestart}-${
						s.lineend
					}</code></li>`
			)
			.join("");
		add(
			"assistant",
			frappe.markdown(reply.message || "") +
				(sources
					? `<details><summary>${__("Sources")}</summary><ul>${sources}</ul></details>`
					: "")
		);
	};
	page.set_primary_action(__("Send"), send);
	$input.on("keydown", (e) => {
		if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
	});
};
