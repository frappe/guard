// Review page: pull request list, overview, tour, full diff, Chat with PR and shortcuts.
// Route: /desk/guard-pr/<Pull Request name>

frappe.pages["guard-pr"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Pull Request Review"),
		single_column: true,
	});
	wrapper.guard_review = new GuardReviewPage(page, wrapper);
};

frappe.pages["guard-pr"].on_page_show = function (wrapper) {
	wrapper.guard_review.show(frappe.get_route()[1]);
};

const SEVERITY = { P0: "red", P1: "orange", P2: "yellow" };
const MODES = ["Ask every time", "Allow routine actions", "Full access"];

class GuardReviewPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.state = {
			list: [],
			listState: "open",
			selected: new Set(),
			tab: "overview",
			view: "tour",
			step: 0,
			file: null,
			chat: null,
			chatOpen: false,
			selecting: false,
			picks: [],
		};
		this.render_shell();
		this.bind_keys();
		frappe.realtime.on("guard_review_updated", (data) => {
			if (data.pull_request === this.name) this.load(this.name);
		});
	}

	// ---------------------------------------------------------------- data

	async show(name) {
		await this.load_list();
		if (name) await this.load(decodeURIComponent(name));
		else if (this.state.list.length) frappe.set_route("guard-pr", this.state.list[0].name);
	}

	async load_list() {
		this.state.list = await frappe.xcall("guard.api.ui.pull_requests", {
			state: this.state.listState,
			search: this.search || "",
		});
		this.render_list();
	}

	async load(name) {
		if (this.name && this.name !== name)
			frappe.realtime.doc_unsubscribe("Pull Request", this.name);
		this.name = name;
		// Review updates for this pull request arrive in its document room.
		frappe.realtime.doc_subscribe("Pull Request", name);
		this.data = await frappe.xcall("guard.api.ui.pull_request_view", { pull_request: name });
		const pr = this.data.pull_request;
		this.page.set_title(`#${pr.number} ${pr.title}`);
		this.state.step = 0;
		this.state.file = (this.data.files[0] || {}).path;
		this.render_actions();
		this.render_list();
		this.render_main();
		if (frappe.utils.get_url_arg("fix") === "all") this.fix_all();
	}

	// ----------------------------------------------------------------- shell

	render_shell() {
		$(`<style>
			.gpr { display: grid; grid-template-columns: 260px 1fr; gap: 12px; }
			.gpr.chat-open { grid-template-columns: 260px 1fr 380px; }
			.gpr.no-side { grid-template-columns: 1fr; } .gpr.no-side.chat-open { grid-template-columns: 1fr 380px; }
			.gpr-side { border-right: 1px solid var(--border-color); max-height: 80vh; overflow: auto; }
			.gpr-item { padding: 8px; border-radius: 6px; cursor: pointer; font-size: 13px; }
			.gpr-item.active { background: var(--bg-light-gray); } .gpr-item.focus { outline: 1px solid var(--primary); }
			.gpr-tabs .tab { margin-right: 16px; cursor: pointer; } .gpr-tabs .tab.active { font-weight: 600; border-bottom: 2px solid var(--primary); }
			.gpr-diff { font-family: var(--font-family-monospace, monospace); font-size: 12px; white-space: pre; overflow-x: auto; }
			.gpr-diff .add { background: var(--green-50, #e6ffec); } .gpr-diff .del { background: var(--red-50, #ffebe9); }
			.gpr-diff .ln { color: var(--text-muted); display: inline-block; width: 44px; text-align: right; padding-right: 8px; user-select: none; }
			.gpr-diff .row.picked { outline: 2px solid var(--blue-500, #3b82f6); }
			.gpr-finding { border-left: 3px solid var(--orange-500); background: var(--bg-light-gray); padding: 8px 12px; margin: 4px 0 8px 52px; white-space: normal; font-family: var(--font-family); }
			.gpr-chat { border-left: 1px solid var(--border-color); display: flex; flex-direction: column; height: 80vh; }
			.gpr-chat .msgs { flex: 1; overflow: auto; padding: 8px; } .gpr-chat .msg { margin-bottom: 10px; }
			.gpr-chat .msg.user { text-align: right; } .gpr-chat .chips span { display: inline-block; background: var(--bg-blue); border-radius: 10px; padding: 0 8px; margin: 2px; font-size: 11px; }
			.gpr-picking .gpr-pickable:hover { outline: 2px dashed var(--blue-500, #3b82f6); cursor: crosshair; }
			.gpr-files .file { display: block; font-size: 12px; padding: 2px 4px; cursor: pointer; } .gpr-files .file.active { font-weight: 600; }
		</style>`).appendTo(this.page.main);
		this.$root = $(`<div class="gpr">
			<div class="gpr-side"><input class="form-control input-sm gpr-search" placeholder="${__(
				"Search (/)"
			)}"><div class="gpr-list"></div></div>
			<div class="gpr-main"></div>
			<div class="gpr-chat" style="display:none"></div>
		</div>`).appendTo(this.page.main);
		this.$root.find(".gpr-search").on(
			"input",
			frappe.utils.debounce((e) => {
				this.search = e.target.value;
				this.load_list();
			}, 300)
		);
	}

	render_actions() {
		this.page.clear_actions_menu();
		this.page.clear_primary_action();
		this.page.set_primary_action(
			__("Chat with PR"),
			() => this.toggle_chat(),
			"message-square"
		);
		this.page.add_action_item(__("Re-review (G)"), () => this.rereview());
		this.page.add_action_item(__("Approve (A)"), () => this.provider_action("approve"));
		this.page.add_action_item(__("Request changes (X)"), () =>
			this.provider_action("request_changes")
		);
		this.page.add_action_item(__("Merge (M)"), () => this.provider_action("merge"));
		this.page.add_action_item(__("Close (C)"), () => this.provider_action("close"));
		this.page.add_action_item(__("Reopen (O)"), () => this.provider_action("reopen"));
		this.page.add_action_item(__("Update branch (U)"), () =>
			this.provider_action("update_branch")
		);
		this.page.add_action_item(__("Fix all with your agent (F)"), () => this.fix_all());
		this.page.add_action_item(__("Open on GitHub"), () =>
			window.open(this.data.pull_request.web_url)
		);
		this.page.add_action_item(__("Keyboard shortcuts (?)"), () => this.show_shortcuts());
	}

	render_list() {
		const $list = this.$root.find(".gpr-list").empty();
		$(
			`<div class="text-muted small" style="margin:6px 0">${__(
				this.state.listState === "open" ? "Open" : "Closed"
			)} · Shift+O</div>`
		).appendTo($list);
		this.state.list.forEach((pr, i) => {
			const score =
				pr.confidence_score != null && pr.review_count ? `${pr.confidence_score}/5` : "–";
			const $item = $(`<div class="gpr-item ${pr.name === this.name ? "active" : ""} ${
				this.state.selected.has(pr.name) ? "focus" : ""
			}">
				<div><b>#${pr.number}</b> ${frappe.utils.escape_html(pr.title)}</div>
				<div class="text-muted">${frappe.utils.escape_html(pr.author || "")} · ${score}${
				pr.is_draft ? " · draft" : ""
			}</div></div>`);
			$item.on("click", () => frappe.set_route("guard-pr", pr.name));
			$item.data("index", i);
			$list.append($item);
		});
	}

	render_main() {
		const $main = this.$root.find(".gpr-main").empty();
		const tabs = [
			["overview", __("Overview")],
			["code", __("Code")],
			["reviews", __("Reviews")],
		];
		const $tabs = $(`<div class="gpr-tabs" style="margin-bottom:12px"></div>`).appendTo($main);
		tabs.forEach(([key, label]) =>
			$(
				`<span role="button" class="tab ${
					this.state.tab === key ? "active" : ""
				}">${label}</span>`
			)
				.on("click", () => {
					this.state.tab = key;
					this.render_main();
				})
				.appendTo($tabs)
		);
		if (this.state.tab === "overview") this.render_overview($main);
		else if (this.state.tab === "code") this.render_code($main);
		else this.render_reviews($main);
		this.$root.toggleClass("gpr-picking", this.state.selecting);
	}

	// -------------------------------------------------------------- overview

	render_overview($main) {
		const review = this.data.review;
		if (!review) {
			$main.append(`<p class="text-muted">${__("No review yet.")}</p>`);
			return;
		}
		const findings = this.data.findings;
		const rows = findings
			.map(
				(f) => `<tr class="gpr-pickable" data-finding="${
					f.name
				}"><td><span class="indicator-pill ${SEVERITY[f.severity]}">${
					f.severity
				}</span></td>
			<td><a href="/desk/review-finding/${f.name}">${frappe.utils.escape_html(
					f.title
				)}</a></td><td><code>${f.path}:${f.start_line}</code></td><td>${
					f.status
				}</td><td>👍 ${f.upvotes || 0} 👎 ${f.downvotes || 0}</td></tr>`
			)
			.join("");
		$main.append(`<div class="gpr-pickable" data-label="summary">
			<h4>${__("Confidence")}: ${review.confidence_score}/5 · ${__("Risk")}: ${
			review.risk_level || "–"
		} ${review.tier && review.tier !== "base" ? `· ${review.tier}` : ""}</h4>
			<div>${frappe.markdown(review.summary || "")}</div></div>
			<h5 style="margin-top:16px">${__("Findings")} (${findings.length})</h5>
			<table class="table table-sm"><tbody>${
				rows || `<tr><td class="text-muted">${__("No findings.")}</td></tr>`
			}</tbody></table>`);
		if (review.diagram) this.render_diagram($main, review.diagram);
		this.bind_picks($main);
	}

	async render_diagram($main, source) {
		const $box = $(
			`<div style="margin-top:16px"><h5>${__(
				"Diagram"
			)}</h5><pre class="gpr-mermaid">${frappe.utils.escape_html(source)}</pre></div>`
		).appendTo($main);
		try {
			const mermaid = (
				await import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")
			).default;
			mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
			const { svg } = await mermaid.render(`gpr-${Date.now()}`, source);
			$box.find("pre").replaceWith(svg);
		} catch (e) {
			// Offline: keep the Mermaid source.
		}
	}

	// ------------------------------------------------------------------ code

	render_code($main) {
		const tour = this.data.tour || [];
		const hasTour = tour.length > 0;
		if (!hasTour) this.state.view = "diff";
		const $switch = $(`<div style="margin-bottom:8px"></div>`).appendTo($main);
		if (hasTour) {
			[
				["tour", __("Tour")],
				["diff", __("Full diff")],
			].forEach(([key, label]) =>
				$(
					`<button class="btn btn-xs ${
						this.state.view === key ? "btn-primary" : "btn-default"
					}" style="margin-right:6px">${label}</button>`
				)
					.on("click", () => {
						this.state.view = key;
						this.render_main();
					})
					.appendTo($switch)
			);
		}
		if (this.state.view === "tour") this.render_tour($main, tour);
		else this.render_full_diff($main);
		this.bind_picks($main);
	}

	render_tour($main, tour) {
		const step = tour[Math.min(this.state.step, tour.length - 1)];
		const hunks = new Set(step.hunks);
		$main.append(`<div><b>${__("Step")} ${this.state.step + 1}/${
			tour.length
		}: ${frappe.utils.escape_html(step.title)}</b> <span class="text-muted">(← →)</span>
			<div style="margin:6px 0 12px">${frappe.markdown(step.explanation || "")}</div></div>`);
		this.data.files.forEach((file) => {
			const parts = file.hunks.filter((h) => hunks.has(h.id));
			if (parts.length) $main.append(this.file_html(file, parts));
		});
	}

	render_full_diff($main) {
		const $wrap = $(
			`<div style="display:grid;grid-template-columns:220px 1fr;gap:12px"><div class="gpr-files"></div><div class="gpr-file"></div></div>`
		).appendTo($main);
		this.data.files.forEach((file) => {
			$(
				`<span role="button" class="file ${
					file.path === this.state.file ? "active" : ""
				}">${frappe.utils.escape_html(file.path)} <span class="text-muted">+${
					file.additions
				} -${file.deletions}</span></span>`
			)
				.on("click", () => {
					this.state.file = file.path;
					this.render_main();
				})
				.appendTo($wrap.find(".gpr-files"));
		});
		const file = this.data.files.find((f) => f.path === this.state.file);
		if (file) $wrap.find(".gpr-file").append(this.file_html(file, file.hunks));
	}

	file_html(file, hunks) {
		const byLine = {};
		this.data.findings
			.filter((f) => f.path === file.path)
			.forEach((f) => {
				const key = `${f.side === "LEFT" ? "L" : "R"}${f.end_line}`;
				(byLine[key] = byLine[key] || []).push(f);
			});
		let html = `<div class="gpr-pickable" data-label="${frappe.utils.escape_html(
			file.path
		)}"><h6 style="margin-top:12px"><code>${frappe.utils.escape_html(
			file.path
		)}</code> <span class="text-muted">${file.status}</span></h6></div><div class="gpr-diff">`;
		hunks.forEach((h) => {
			html += `<div class="text-muted">@@ ${frappe.utils.escape_html(h.header || "")}</div>`;
			h.lines.forEach(([kind, oldNo, newNo, text]) => {
				const cls = kind === "+" ? "add" : kind === "-" ? "del" : "";
				html += `<div class="row ${cls}" data-path="${frappe.utils.escape_html(
					file.path
				)}" data-line="${newNo || ""}"><span class="ln">${
					oldNo || ""
				}</span><span class="ln">${newNo || ""}</span>${kind} ${frappe.utils.escape_html(
					text
				)}</div>`;
				const here = kind === "-" ? byLine[`L${oldNo}`] : byLine[`R${newNo}`];
				(here || []).forEach((f) => {
					html += `<div class="gpr-finding gpr-pickable" data-finding="${
						f.name
					}"><span class="indicator-pill ${SEVERITY[f.severity]}">${
						f.severity
					}</span> <b>${frappe.utils.escape_html(f.title)}</b><div>${frappe.markdown(
						f.body || ""
					)}</div></div>`;
				});
			});
		});
		return html + "</div>";
	}

	render_reviews($main) {
		const rows = this.data.reviews
			.map(
				(r) =>
					`<tr><td><a href="/desk/code-review/${r.name}">${r.name}</a></td><td>${
						r.status
					}</td><td>${r.trigger || ""}</td><td>${r.tier || ""}</td><td>${
						r.confidence_score ?? ""
					}</td><td>${r.posted_count ?? ""}</td><td>${frappe.datetime.comment_when(
						r.creation
					)}</td></tr>`
			)
			.join("");
		$main.append(
			`<table class="table table-sm"><thead><tr><th>${__("Review")}</th><th>${__(
				"Status"
			)}</th><th>${__("Trigger")}</th><th>${__("Tier")}</th><th>${__("Score")}</th><th>${__(
				"Comments"
			)}</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
		);
	}

	// ------------------------------------------------------------------ chat

	toggle_chat(force) {
		this.state.chatOpen = force === undefined ? !this.state.chatOpen : force;
		this.$root.toggleClass("chat-open", this.state.chatOpen);
		const $chat = this.$root.find(".gpr-chat").toggle(this.state.chatOpen);
		if (this.state.chatOpen && !$chat.children().length) this.render_chat($chat);
		if (this.state.chatOpen) $chat.find("textarea").focus();
	}

	render_chat($chat) {
		$chat.html(`<div class="msgs"></div>
			<div class="chips" style="padding:4px 8px"></div>
			<div style="padding:8px;border-top:1px solid var(--border-color)">
				<textarea class="form-control" rows="3" placeholder="${__(
					"Ask about this pull request"
				)}"></textarea>
				<div style="margin-top:6px;display:flex;gap:6px;align-items:center">
					<button class="btn btn-xs btn-default select">${__("Select context")} (Shift+S)</button>
					<button class="btn btn-xs btn-default mode" title="${__("Action permissions")}">🛡 ${__(
			MODES[0]
		)}</button>
					<span style="flex:1"></span>
					<button class="btn btn-xs btn-primary send">${__("Send")}</button>
				</div>
			</div>`);
		$chat.find(".send").on("click", () => this.send());
		$chat.find(".select").on("click", () => this.toggle_select());
		$chat.find(".mode").on("click", () => this.cycle_mode());
		$chat.find("textarea").on("keydown", (e) => {
			if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) this.send();
		});
	}

	async send() {
		const $chat = this.$root.find(".gpr-chat");
		const text = $chat.find("textarea").val().trim();
		if (!text) return;
		this.add_message("user", text);
		$chat.find("textarea").val("");
		const context = this.state.picks;
		this.state.picks = [];
		this.state.selecting = false;
		this.render_chips();
		this.$root.removeClass("gpr-picking");
		const reply = await frappe.xcall("guard.conversation.chat.send", {
			pull_request: this.name,
			message: text,
			session: this.state.chat,
			context,
		});
		this.state.chat = reply.session;
		this.add_message("assistant", reply.answer);
		reply.actions.forEach((a) => this.add_action(a));
	}

	add_message(role, text) {
		this.$root
			.find(".gpr-chat .msgs")
			.append(
				`<div class="msg ${role}">${
					role === "user" ? frappe.utils.escape_html(text) : frappe.markdown(text || "")
				}</div>`
			)
			.scrollTop(1e9);
	}

	add_action(action) {
		const $card = $(`<div class="msg"><div class="frappe-card" style="padding:8px">
			<b>${frappe.utils.escape_html(action.action)}</b>: ${frappe.utils.escape_html(
			action.summary || ""
		)} ${action.draft ? `<span class="text-muted">(${__("draft")})</span>` : ""}
			${
				action.payload.body !== undefined
					? `<textarea class="form-control" rows="3" style="margin-top:6px">${frappe.utils.escape_html(
							action.payload.body || ""
					  )}</textarea>`
					: ""
			}
			<div class="status text-muted small" style="margin-top:4px">${action.status}</div>
			<div class="buttons" style="margin-top:6px">
				<button class="btn btn-xs btn-primary approve">${__("Approve")}</button>
				${
					action.routine
						? `<button class="btn btn-xs btn-default allow">${__(
								"Allow for this chat"
						  )}</button>`
						: ""
				}
				<button class="btn btn-xs btn-default cancel">${__("Cancel")}</button></div></div></div>`);
		const decide = async (approve, allow) => {
			const body = $card.find("textarea").length ? $card.find("textarea").val() : null;
			const result = await frappe.xcall("guard.conversation.chat.decide", {
				action: action.name,
				approve,
				body,
				allow_for_chat: !!allow,
			});
			$card
				.find(".status")
				.text(`${result.status}${result.result ? ": " + result.result : ""}`);
			$card.find(".buttons").remove();
		};
		$card.find(".approve").on("click", () => decide(true));
		$card.find(".allow").on("click", () => decide(true, true));
		$card.find(".cancel").on("click", () => decide(false));
		if (action.status !== "Proposed" || action.draft) $card.find(".approve, .allow").remove();
		this.$root.find(".gpr-chat .msgs").append($card).scrollTop(1e9);
	}

	async cycle_mode() {
		if (!this.state.chat) {
			frappe.show_alert(__("Send a message first."));
			return;
		}
		const $btn = this.$root.find(".gpr-chat .mode");
		const next = MODES[(MODES.indexOf(this.mode || MODES[0]) + 1) % MODES.length];
		if (
			next === "Full access" &&
			!(await new Promise((r) =>
				frappe.confirm(
					__(
						"Full access runs every action without asking, including merges. Continue?"
					),
					() => r(true),
					() => r(false)
				)
			))
		)
			return;
		const result = await frappe.xcall("guard.conversation.chat.set_mode", {
			session: this.state.chat,
			mode: next,
		});
		this.mode = next;
		$btn.text(`🛡 ${__(next)}`);
		result.ran.forEach((r) =>
			this.add_message("assistant", `${__("Ran")} ${r.name}: ${r.status}`)
		);
	}

	toggle_select(force) {
		this.state.selecting = force === undefined ? !this.state.selecting : force;
		this.$root.toggleClass("gpr-picking", this.state.selecting);
		this.$root
			.find(".gpr-chat .select")
			.toggleClass("btn-primary", this.state.selecting)
			.text(this.state.selecting ? __("Done") : `${__("Select context")} (Shift+S)`);
	}

	bind_picks($main) {
		$main.find(".gpr-pickable").on("click", (e) => {
			if (!this.state.selecting) return;
			e.preventDefault();
			const $el = $(e.currentTarget);
			const finding = this.data.findings.find((f) => f.name === $el.data("finding"));
			const label = finding
				? `${finding.severity} ${finding.title}`
				: $el.data("label") || "selection";
			this.state.picks.push({
				label,
				text: finding
					? `${finding.path}:${finding.start_line} ${finding.title}\n${finding.body}`
					: $el.text().trim().slice(0, 4000),
			});
			this.render_chips();
		});
		let start = null;
		$main.find(".gpr-diff .row").on("mousedown", (e) => {
			if (this.state.selecting) start = $(e.currentTarget);
		});
		$main.find(".gpr-diff .row").on("mouseup", (e) => {
			if (!this.state.selecting || !start) return;
			const $rows = start.parent().children(".row");
			const [a, b] = [$rows.index(start), $rows.index(e.currentTarget)].sort(
				(x, y) => x - y
			);
			const picked = $rows.slice(a, b + 1).addClass("picked");
			const path = start.data("path");
			this.state.picks.push({
				label: `${path}:${picked.first().data("line") || ""}`,
				text: picked
					.map((_, r) => $(r).text())
					.get()
					.join("\n"),
			});
			start = null;
			this.render_chips();
		});
	}

	render_chips() {
		this.$root
			.find(".gpr-chat .chips")
			.html(
				this.state.picks
					.map((p) => `<span>${frappe.utils.escape_html(p.label)}</span>`)
					.join("")
			);
	}

	// --------------------------------------------------------------- actions

	async rereview() {
		const name = await frappe.xcall("guard.api.ui.rereview", { pull_request: this.name });
		frappe.show_alert({
			message: name ? __("Review {0} queued", [name]) : __("Review skipped"),
			indicator: name ? "green" : "orange",
		});
	}

	async provider_action(action) {
		const targets = this.state.selected.size ? [...this.state.selected] : [this.name];
		const run = async (body) => {
			for (const target of targets) {
				const result = await frappe.xcall("guard.conversation.chat.direct", {
					pull_request: target,
					action,
					body: body || "",
				});
				frappe.show_alert(
					{
						message: `${action}: ${result.status}${
							result.result && result.status !== "Executed"
								? " · " + result.result
								: ""
						}`,
						indicator: result.status === "Executed" ? "green" : "red",
					},
					7
				);
			}
			this.load(this.name);
		};
		if (action === "request_changes")
			frappe.prompt(
				{ fieldname: "body", fieldtype: "Small Text", label: __("Comment"), reqd: 1 },
				(v) => run(v.body),
				__("Request changes")
			);
		else
			frappe.confirm(
				__("Run {0} on {1} pull request(s) as you on GitHub?", [action, targets.length]),
				() => run()
			);
	}

	async fix_all() {
		const result = await frappe.xcall("guard.api.cli.fix_prompt", { target: this.name });
		frappe.utils.copy_to_clipboard(result.prompt);
		const url = `guard-fix://open?target=${encodeURIComponent(this.name)}&agent=claude`;
		frappe.msgprint(
			`${__("The fix prompt is on your clipboard.")}<br><br><a href="${url}">${__(
				"Open in Claude Code"
			)}</a> · <a href="guard-fix://open?target=${encodeURIComponent(
				this.name
			)}&agent=codex">Codex</a> · <a href="cursor://anysphere.cursor-deeplink/prompt?text=${encodeURIComponent(
				result.prompt.slice(0, 6000)
			)}">Cursor</a>`,
			__("Fix all")
		);
	}

	show_shortcuts() {
		const keys = [
			["J / K", "Next / previous pull request"],
			["H / L", "Previous / next tab"],
			["← / →", "Previous / next tour step"],
			["/", "Search"],
			["Shift+O", "Open / closed list"],
			["Ctrl+B", "Toggle sidebar"],
			["Shift+C", "Open chat"],
			["Shift+S", "Select chat context"],
			["Space", "Select the focused pull request"],
			["Ctrl+A", "Select all visible"],
			["G", "Re-review"],
			["A", "Approve"],
			["M", "Merge"],
			["C", "Close"],
			["D", "Draft / ready"],
			["O", "Reopen"],
			["X", "Request changes"],
			["U", "Update branch"],
			["F", "Fix"],
			["R", "Toggle reviewed (hovered file)"],
			["T", "Comment on the hovered line"],
			["Esc", "Leave select mode"],
		];
		frappe.msgprint(
			`<table class="table table-sm">${keys
				.map(([k, v]) => `<tr><td><kbd>${k}</kbd></td><td>${__(v)}</td></tr>`)
				.join("")}</table>`,
			__("Keyboard shortcuts")
		);
	}

	// ------------------------------------------------------------------ keys

	bind_keys() {
		$(document).on("keydown.guardpr", (e) => {
			if (frappe.get_route()[0] !== "guard-pr" || !this.data) return;
			const typing = $(e.target).is("input, textarea, [contenteditable]");
			if (typing) {
				if (e.key === "Escape") e.target.blur();
				return;
			}
			const list = this.state.list;
			const index = list.findIndex((p) => p.name === this.name);
			const key = e.key;
			const map = {
				j: () => list[index + 1] && frappe.set_route("guard-pr", list[index + 1].name),
				k: () => list[index - 1] && frappe.set_route("guard-pr", list[index - 1].name),
				h: () => this.switch_tab(-1),
				l: () => this.switch_tab(1),
				ArrowRight: () => this.move_step(1),
				ArrowLeft: () => this.move_step(-1),
				"/": () => this.$root.find(".gpr-search").focus(),
				O: () => {
					this.state.listState = this.state.listState === "open" ? "closed" : "open";
					this.load_list();
				},
				C: () => e.shiftKey && this.toggle_chat(),
				S: () => e.shiftKey && (this.toggle_chat(true), this.toggle_select()),
				" ": () => {
					this.state.selected.has(this.name)
						? this.state.selected.delete(this.name)
						: this.state.selected.add(this.name);
					this.render_list();
				},
				g: () => this.rereview(),
				a: () =>
					e.ctrlKey || e.metaKey ? this.select_all() : this.provider_action("approve"),
				m: () => this.provider_action("merge"),
				c: () => this.provider_action("close"),
				o: () => this.provider_action("reopen"),
				x: () => this.provider_action("request_changes"),
				u: () => this.provider_action("update_branch"),
				d: () =>
					!this.state.selected.size &&
					this.provider_action(this.data.pull_request.is_draft ? "ready" : "draft"),
				f: () => this.fix_all(),
				t: () => this.comment_hovered(),
				r: () => this.toggle_reviewed(),
				b: () =>
					(e.ctrlKey || e.metaKey) &&
					this.$root.toggleClass("no-side").find(".gpr-side").toggle(),
				"?": () => this.show_shortcuts(),
				Escape: () => this.toggle_select(false),
			};
			const action = map[key];
			if (action) {
				e.preventDefault();
				action();
			}
		});
	}

	switch_tab(delta) {
		const tabs = ["overview", "code", "reviews"];
		this.state.tab = tabs[(tabs.indexOf(this.state.tab) + delta + tabs.length) % tabs.length];
		this.render_main();
	}

	move_step(delta) {
		const total = (this.data.tour || []).length;
		if (this.state.tab !== "code" || this.state.view !== "tour" || !total) return;
		this.state.step = Math.max(0, Math.min(total - 1, this.state.step + delta));
		this.render_main();
	}

	select_all() {
		const all = this.state.list.every((p) => this.state.selected.has(p.name));
		this.state.list.forEach((p) =>
			all ? this.state.selected.delete(p.name) : this.state.selected.add(p.name)
		);
		this.render_list();
	}

	comment_hovered() {
		const $row = this.$root.find(".gpr-diff .row:hover").first();
		if (!$row.length || !$row.data("line")) return;
		frappe.prompt(
			{
				fieldname: "body",
				fieldtype: "Small Text",
				label: __("Comment on {0}:{1}", [$row.data("path"), $row.data("line")]),
				reqd: 1,
			},
			async (v) => {
				const body = `${v.body}\n\n_(on \`${$row.data("path")}:${$row.data("line")}\`)_`;
				const result = await frappe.xcall("guard.conversation.chat.direct", {
					pull_request: this.name,
					action: "post_comment",
					body,
				});
				frappe.show_alert({
					message: `${__("Comment")}: ${result.status}`,
					indicator: result.status === "Executed" ? "green" : "red",
				});
			}
		);
	}

	toggle_reviewed() {
		const $file = this.$root.find(".gpr-files .file:hover, .gpr-pickable h6:hover").first();
		$file.toggleClass("text-muted");
	}
}
