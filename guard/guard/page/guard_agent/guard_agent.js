// Connect Your Agent: one command that sets up Claude Code, Codex and pi, a prompt for other agents,
// and a status line that turns green when the agent calls Guard for the first time.

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

	const asks = [
		[
			__("Review my change with Guard."),
			__("before a push, with the team rules and the code graph"),
		],
		[
			__("Fix Guard's findings on my PR."),
			__("the open findings of the pull request of the branch"),
		],
		[
			__("Guard is wrong about this: …"),
			__("dismisses the finding, and Guard learns the reason"),
		],
		[__("Who calls this in other repositories?"), __("the code graph of every repository")],
		[
			__("What are the team rules for this file?"),
			__("rules, instructions and learned memories"),
		],
	];

	const render = () => {
		const examples = asks
			.map(
				([say, what]) =>
					`<li>“${esc(say)}” <span class="text-muted">— ${esc(what)}</span></li>`
			)
			.join("");
		$body.html(`
			<div class="mt-2">${status()}</div>
			${oauth()}
			${heading(__("1. Choose an agent to install"))}
			<p class="text-muted">${__(
				"Choose one or more agents, then copy the command. You can also pass --client NAME to the installer."
			)}</p>
			${block("install", data.install_all)}
			<div class="form-group mb-3">
				<label class="control-label">${__("Agents")}</label>
				<div class="guard-agent-choices">
					${["claude", "codex", "pi"]
						.map(
							(client) => `<label class="checkbox-inline mr-3">
								<input type="checkbox" data-agent="${client}"> ${__(client)}
							</label>`
						)
						.join("")}
				</div>
				<button class="btn btn-default btn-xs mt-2" data-select="all">${__("Select all")}</button>
				<button class="btn btn-default btn-xs mt-2" data-select="none">${__("Clear")}</button>
			</div>
			${heading(__("2. Ask your agent"))}
			<ul class="mb-2">${examples}</ul>
			<p class="text-muted">${__(
				"Your agent reviews with its own model. Guard gives it the team rules, the code graph and the findings of its checks, and saves the result here. Nothing is posted to GitHub. In Claude Code, /mcp__guard__review does the same."
			)}</p>
			${heading(__("3. Stop pushes with P0 findings (optional)"))}
			<p class="text-muted">${__(
				"Run this in a repository. Before each push, your agent reviews the pushed commits with Guard, and a P0 finding stops the push. To skip it once: GUARD_SKIP=1 git push"
			)}</p>
			${block("install_hook", data.install_hook)}
			${heading(__("Other agents"))}
			<p class="text-muted">${__(
				"Paste this prompt into Cursor, VS Code or another agent. It adds Guard and tells you how to sign in."
			)}</p>
			${block("prompt", data.prompt)}
			<p class="text-muted mb-1">${__(
				"Or add a streamable HTTP server with this URL and sign in with OAuth:"
			)}</p>
			${block("endpoint", data.endpoint)}
			${heading(__("Your local reviews"))}
			${reviews()}
		`);
		const $choices = $body.find("[data-agent]");
		$choices.prop("checked", false);
		const updateInstall = () => {
			const selected = $choices
				.filter(":checked")
				.map((_, el) => $(el).data("agent"))
				.get();
			$body
				.find('[data-copy="install"]')
				.data(
					"copy-value",
					selected.length
						? `${data.install} --client ${selected.join(" --client ")}`
						: data.install_all
				);
		};
		$choices.on("change", updateInstall);
		$body.find("[data-select]").on("click", (e) => {
			const all = $(e.currentTarget).data("select") === "all";
			$choices.prop("checked", all);
			updateInstall();
		});
		updateInstall();
		$body.find("[data-copy]").on("click", (e) => {
			const $button = $(e.currentTarget);
			frappe.utils.copy_to_clipboard(
				$button.data("copy-value") || data[$button.attr("data-copy")]
			);
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
