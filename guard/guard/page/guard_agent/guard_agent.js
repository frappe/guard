// Connect Your Agent: one prompt that the coding agent of the developer follows to add the MCP server
// of Guard, and a status line that turns green when the agent calls Guard for the first time.

frappe.pages["guard-agent"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Connect Your Agent"),
		single_column: true,
	});
	const $body = $(
		`<div style="padding: var(--padding-sm) var(--padding-md) var(--padding-lg); max-width: 54rem"></div>`
	).appendTo(page.main);
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	// Ask again until the agent connects. The server keeps the time of the last call of the agent.
	const POLL_MS = 5000;
	let timer = null;
	let data = null;

	const heading = (title) => `<h5 class="mt-5 mb-2">${esc(title)}</h5>`;
	const block = (key, text) => `
		<div style="position: relative">
			<pre class="mb-2" style="white-space: pre-wrap; padding-right: 5rem">${esc(text)}</pre>
			<button class="btn btn-default btn-xs" data-copy="${key}"
				style="position: absolute; top: var(--padding-sm); right: var(--padding-sm)">${__("Copy")}</button>
		</div>`;

	const status = () => {
		const seen = data.seen;
		if (!seen) {
			return `<div class="indicator-pill orange">${__("Not connected yet")}</div>
				<span class="text-muted ml-2">${__("This page updates when your agent calls Guard.")}</span>`;
		}
		const client = seen.client ? `${esc(seen.client)}, ` : "";
		return `<div class="indicator-pill green">${__("Connected")}</div>
			<span class="text-muted ml-2">${__("Last call: {0}{1}", [
				client,
				esc(frappe.datetime.prettyDate(seen.at)),
			])}</span>`;
	};

	const oauth = () => {
		if (!data.oauth_off.length) return "";
		const fix = data.can_fix_oauth
			? `<a href="/desk/oauth-settings">${__("Open OAuth Settings")}</a>`
			: __("Ask an admin to turn them on in OAuth Settings.");
		return `<div class="alert alert-warning mt-3">${__(
			"Agents cannot sign in. These OAuth settings are off: {0}.",
			[data.oauth_off.map(esc).join(", ")]
		)} ${fix}</div>`;
	};

	const reviews = () => {
		if (!data.reviews.length) {
			return `<p class="text-muted">${__(
				"No local reviews yet. Ask your agent: “Review my change with Guard.”"
			)}</p>`;
		}
		const rows = data.reviews
			.map(
				(r) => `<tr>
					<td><a href="/desk/code-review/${encodeURIComponent(r.name)}">${esc(r.name)}</a></td>
					<td>${esc(r.repository)}</td>
					<td>${esc(r.branch)}</td>
					<td>${esc(r.findings_count)}</td>
					<td>${esc(frappe.datetime.prettyDate(r.creation))}</td>
				</tr>`
			)
			.join("");
		return `<table class="table table-bordered table-sm"><thead><tr>
				<th>${__("Review")}</th><th>${__("Repository")}</th><th>${__("Branch")}</th>
				<th>${__("Findings")}</th><th>${__("When")}</th>
			</tr></thead><tbody>${rows}</tbody></table>`;
	};

	const render = () => {
		const claude = `claude mcp add --transport http --scope user guard ${data.endpoint}`;
		const codex = `codex mcp add guard --url ${data.endpoint}\ncodex mcp login guard`;
		$body.html(`
			<div class="mt-2">${status()}</div>
			${oauth()}
			${heading(__("1. Give this prompt to your coding agent"))}
			<p class="text-muted">${__(
				"Paste it into Claude Code, Codex, Cursor or another agent. The agent adds Guard and tells you how to sign in. You sign in once, in the browser, with your Guard account."
			)}</p>
			${block("prompt", data.prompt)}
			${heading(__("2. Ask for a review before you push"))}
			<p>${__("Say: “Review my change with Guard.”")}</p>
			<p class="text-muted">${__(
				"Your agent sends the diff to Guard, gets the team rules, the code graph and the findings of Guard's checks, and reviews the change with its own model. The findings are saved here. Nothing is posted to GitHub."
			)}</p>
			${heading(__("Set up by hand"))}
			<p class="text-muted mb-1">${__(
				"Claude Code: run this, then /mcp, select guard and Authenticate."
			)}</p>
			${block("claude", claude)}
			<p class="text-muted mb-1">${__("Codex:")}</p>
			${block("codex", codex)}
			<p class="text-muted mb-1">${__(
				"Other clients: add a streamable HTTP server with this URL and sign in with OAuth."
			)}</p>
			${block("endpoint", data.endpoint)}
			${heading(__("Your local reviews"))}
			${reviews()}
		`);
		const texts = { prompt: data.prompt, claude, codex, endpoint: data.endpoint };
		$body.find("[data-copy]").on("click", (e) => {
			frappe.utils.copy_to_clipboard(texts[$(e.currentTarget).attr("data-copy")]);
		});
	};

	// Only the answer of the last request renders and arms the timer, so one loop runs at a time.
	let seq = 0;
	const load = () => {
		clearTimeout(timer);
		const id = ++seq;
		frappe.call({ method: "guard.api.agent.connect", type: "GET" }).then((r) => {
			if (id !== seq) return;
			data = r.message;
			render();
			if (!data.seen) timer = setTimeout(poll, POLL_MS);
		});
	};

	// A hidden tab asks for nothing. Another page stops the loop, and `on_page_show` starts it again.
	const poll = () => {
		if (frappe.get_route()[0] !== "guard-agent") return;
		if (document.hidden) timer = setTimeout(poll, POLL_MS);
		else load();
	};

	wrapper.guard_load = load;
};

frappe.pages["guard-agent"].on_page_show = function (wrapper) {
	if (wrapper.guard_load) wrapper.guard_load();
};
