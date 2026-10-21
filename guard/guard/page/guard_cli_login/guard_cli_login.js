// `guard login` opens this page. It issues an API key and hands it to the CLI on this computer.
// The callback host is fixed to 127.0.0.1. The CLI prints a one-time code that the user matches
// here, so a crafted link cannot send a key to some other local program unnoticed.

frappe.pages["guard-cli-login"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Sign in the Guard CLI"),
		single_column: true,
	});
	const port = parseInt(frappe.utils.get_url_arg("port"), 10);
	const code = String(frappe.utils.get_url_arg("code") || "");
	if (!Number.isInteger(port) || port < 1024 || port > 65535 || !/^[A-Z0-9]{8}$/.test(code)) {
		page.main.html(`<p>${__("Run <code>guard login</code> in your terminal to start.")}</p>`);
		return;
	}
	page.main.html(`<p>${__(
		"The Guard CLI asks for access as {0}. A new API key replaces your current one.",
		[frappe.session.user]
	)}</p>
		<p>${__(
			"Allow only if your terminal shows this code:"
		)} <b style="font-size:1.4em;letter-spacing:2px">${code}</b></p>`);
	page.set_primary_action(__("Allow"), async () => {
		const keys = await frappe.xcall("guard.api.cli.issue_key");
		const query = new URLSearchParams({ key: keys.key, secret: keys.secret, code });
		window.location.assign(`http://127.0.0.1:${port}/callback?${query}`);
	});
};
