// Connect Your Agent: what Guard does for a coding agent, and the three steps to a first review. The
// page is prose in a reading measure, with one action: a setup prompt with an agent token in it. The
// prompt is never shown on the page, so the token stays off the screen: the button copies it straight
// to the clipboard, and the developer pastes it into their agent. A status line turns green when the
// agent calls Guard.

frappe.pages["guard-agent"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Connect Your Agent"),
		single_column: true,
	});
	const $body = $(`<div class="guard-connect"></div>`).appendTo(page.main);
	$(`<style>
		.guard-connect { max-width: 640px; padding: var(--padding-lg) var(--padding-md) var(--padding-xl); }
		.guard-connect p { margin: 0; }
		.guard-connect .gc-prose { font-size: var(--text-base); line-height: 1.6; color: var(--ink-gray-7); }
		.guard-connect .gc-muted { font-size: var(--text-sm); line-height: 1.5; color: var(--ink-gray-5); }
		.guard-connect section { margin-top: 2.5rem; }
		.guard-connect h2 { font-size: var(--text-lg); font-weight: 600; color: var(--ink-gray-8); margin: 0; }
		.guard-connect ol { list-style: none; padding: 0; margin: 1rem 0 0; display: flex; flex-direction: column; gap: 1rem; }
		.guard-connect li.gc-step { display: flex; gap: 0.75rem; }
		/* A fixed 24px circle: a step with two lines keeps its number on the first line. */
		.guard-connect .gc-number { display: grid; place-items: center; flex-shrink: 0; width: 24px; height: 24px;
			border-radius: 50%; background: var(--surface-gray-2); color: var(--ink-gray-7); font-size: var(--text-xs); font-weight: 500; }
		.guard-connect .gc-title { font-size: var(--text-base); font-weight: 500; color: var(--ink-gray-8); }
		.guard-connect .gc-asks { margin: 0.75rem 0 0; padding-left: 1.1rem; }
		.guard-connect .gc-asks li { margin-top: 0.25rem; }
		.guard-connect table { margin-top: 0.75rem; font-size: var(--text-sm); }
	</style>`).appendTo(page.main);
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	// Ask again until the agent connects. The server keeps the time of the last call of the agent.
	const POLL_MS = 5000;
	let timer = null;
	let data = null;
	// The prompt has the token, and the server keeps only its hash. Keep it in memory until the page
	// reloads so the reader can copy it again, but never put it in the DOM.
	let prompt = null;
	let promptToken = null;
	// The last rendered answer: a poll with the same answer does not render again.
	let shown = null;

	// Copy the in-memory prompt to the clipboard from inside the click that called this, and tell the
	// reader either way. A copy after a server round-trip can lose the user gesture that the clipboard
	// needs, so report a refusal instead of claiming success: the reader then uses "Copy setup prompt".
	const copyPrompt = () => {
		const done = (ok) => {
			frappe.show_alert({
				indicator: ok ? "green" : "orange",
				message: ok
					? __("Setup prompt copied. Paste it into your agent.")
					: __("Could not copy. Click “Copy setup prompt”."),
			});
			return ok;
		};
		if (navigator.clipboard && window.isSecureContext) {
			return navigator.clipboard.writeText(prompt).then(
				() => done(true),
				() => done(false)
			);
		}
		const input = $("<textarea>").appendTo("body").val(prompt);
		input[0].select();
		const ok = document.execCommand("copy");
		input.remove();
		return Promise.resolve(done(ok));
	};

	const status = () => {
		const seen = data.seen;
		if (!seen) {
			return `<span class="indicator-pill orange">${__("Not connected yet")}</span>
				<span class="gc-muted ml-2">${__("This page updates when your agent calls Guard.")}</span>`;
		}
		const client = seen.client ? `${esc(seen.client)}, ` : "";
		return `<span class="indicator-pill green">${__("Connected")}</span>
			<span class="gc-muted ml-2">${__("Last call: {0}{1}", [
				client,
				esc(frappe.datetime.prettyDate(seen.at)),
			])}</span>`;
	};

	const tokens = () => {
		if (!data.tokens.length) return "";
		const rows = data.tokens
			.map(
				(t) => `<tr>
					<td>${esc(frappe.datetime.prettyDate(t.creation))}</td>
					<td>${esc(frappe.datetime.str_to_user(t.expiration_time))}</td>
					<td><button class="btn btn-default btn-xs" data-revoke="${esc(t.name)}">${__(
					"Revoke"
				)}</button></td>
				</tr>`
			)
			.join("");
		return `<section><h2>${__("Agent tokens")}</h2>
			<p class="gc-muted mt-1">${__(
				"Each one works for 90 days, and only on the MCP server. Revoke one when you stop using an agent."
			)}</p>
			<table class="table table-bordered table-sm"><thead><tr>
				<th>${__("Created")}</th><th>${__("Ends")}</th><th></th>
			</tr></thead><tbody>${rows}</tbody></table></section>`;
	};

	const reviews = () => {
		if (!data.reviews.length) {
			return `<p class="gc-muted mt-1">${__(
				"None yet. Ask your agent: “Review my change with Guard.”"
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

	// The three steps, in the order a new user takes them.
	const steps = [
		[
			__("Connect your agent"),
			__(
				"Get the setup prompt below and paste it into Claude Code, Codex, pi, Cursor or any agent with MCP. The prompt has your token in it, and the agent does the rest."
			),
		],
		[
			__("Ask it to review"),
			__(
				"Say “Review my change with Guard.” Your agent reviews with its own model and Guard's rules and graph. Guard keeps the result here and posts nothing to GitHub."
			),
		],
		[
			__("Stop pushes with P0 findings"),
			__(
				"Optional. In a repository, ask your agent to install the Guard pre-push hook. To skip it once: GUARD_SKIP=1 git push"
			),
		],
	];

	const render = () => {
		const list = steps
			.map(
				([title, body], index) => `<li class="gc-step">
					<span class="gc-number" aria-hidden="true">${index + 1}</span>
					<div style="min-width: 0">
						<p class="gc-title">${esc(title)}</p>
						<p class="gc-muted mt-1">${esc(body)}</p>
					</div>
				</li>`
			)
			.join("");
		const examples = asks
			.map(
				([say, what]) =>
					`<li>“${esc(say)}” <span class="gc-muted">— ${esc(what)}</span></li>`
			)
			.join("");
		// One action: the next step the reader can take. The prompt never goes into the DOM; the button
		// copies it straight to the clipboard.
		const action = `<button class="btn btn-${prompt ? "default" : "primary"} btn-sm mt-4"
			data-action="prompt">${prompt ? __("New setup prompt") : __("Get the setup prompt")}</button>`;
		const setup = prompt
			? `<div class="mt-4">
					<button class="btn btn-primary btn-sm" data-action="copy">${__("Copy setup prompt")}</button>
					${action}
				</div>
				<p class="gc-muted mt-2">${__(
					"Paste the prompt into your agent. Guard never shows the token: it is in the prompt only, and your agent keeps it in its MCP config."
				)}</p>`
			: action;
		$body.html(`
			<p class="gc-prose">${__(
				"Guard reviews your change before you push, with the agent you already use. There is nothing to install: your agent reads the team rules, the code graph of every repository and what earlier reviews found from Guard over MCP, and reviews with its own model."
			)}</p>
			<section>
				<h2>${__("Start")}</h2>
				<ol>${list}</ol>
				${setup}
				<div class="mt-4">${status()}</div>
			</section>
			<section>
				<h2>${__("What to ask")}</h2>
				<ul class="gc-asks gc-prose">${examples}</ul>
			</section>
			${tokens()}
			<section>
				<h2>${__("Your local reviews")}</h2>
				${reviews()}
			</section>
			<section>
				<h2>${__("Something is broken")}</h2>
				<p class="gc-prose mt-1">${__(
					"Ask a Guard admin. Name your agent and paste the error it printed. Never paste your token: revoke it above and get a new prompt instead."
				)}</p>
			</section>
		`);
		$body.find('[data-action="prompt"]').on("click", () => {
			frappe.call({ method: "guard.api.agent.new_token", freeze: true }).then((r) => {
				prompt = r.message.prompt;
				promptToken = r.message.token;
				// Copy it now, while the click still counts as a user gesture.
				copyPrompt();
				load();
			});
		});
		$body.find("[data-revoke]").on("click", (e) => {
			const name = $(e.currentTarget).attr("data-revoke");
			frappe.confirm(
				__("Revoke this token? The agent that has it cannot call Guard any more."),
				() =>
					frappe
						.call({ method: "guard.api.agent.revoke_token", args: { name } })
						.then(() => {
							// A revoked token makes the prompt on the page useless.
							if (name === promptToken) prompt = promptToken = null;
							load();
						})
			);
		});
		$body.find('[data-action="copy"]').on("click", () => copyPrompt());
	};

	// Only the answer of the last request renders and arms the timer, so one loop runs at a time.
	let seq = 0;
	const load = () => {
		clearTimeout(timer);
		const id = ++seq;
		frappe.call({ method: "guard.api.agent.connect", type: "GET" }).then((r) => {
			if (id !== seq) return;
			data = r.message;
			const key = JSON.stringify([data, prompt]);
			if (key !== shown) render();
			shown = key;
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
