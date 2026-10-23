// Guard Health: queues, workers, reviews, the AI gateway and the indexes, in one view.

frappe.pages["guard-health"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Guard Health"),
		single_column: true,
	});
	const $body = $(`<div></div>`).appendTo(page.main);
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

	const table = (title, columns, rows) => {
		if (!rows.length)
			return `<h5 class="mt-4">${esc(title)}</h5><p class="text-muted">${__("Nothing to show.")}</p>`;
		const head = columns.map(([, label]) => `<th>${esc(label)}</th>`).join("");
		const body = rows
			.map((row) => `<tr>${columns.map(([key]) => `<td>${esc(row[key])}</td>`).join("")}</tr>`)
			.join("");
		return `<h5 class="mt-4">${esc(title)}</h5>
			<table class="table table-bordered table-sm"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
	};

	const render = (data) => {
		const rate = data.failure_rate;
		const gateway = data.gateway;
		const alerts = [];
		if (gateway.paused_for)
			alerts.push(
				__("Reviews are paused: the AI gateway is down since {0}. Last error: {1}", [
					esc(gateway.outage_since),
					esc(gateway.last_error),
				]),
			);
		if (rate.ended && rate.rate > 0.2)
			alerts.push(__("{0} of {1} reviews failed in the last hour.", [rate.failed, rate.ended]));
		const banner = alerts.length
			? `<div class="alert alert-danger">${alerts.join("<br>")}</div>`
			: `<div class="alert alert-success">${__("No problems found.")}</div>`;
		const reviews = Object.entries(data.reviews).map(([status, count]) => ({ status, count }));
		$body.html(
			banner +
				table(
					__("Queues"),
					[
						["queue", __("Queue")],
						["queued", __("Waiting")],
						["running", __("Running")],
						["workers", __("Workers")],
						["error", __("Error")],
					],
					data.queues,
				) +
				table(
					__("Reviews in the last 24 hours"),
					[
						["status", __("Status")],
						["count", __("Reviews")],
					],
					reviews,
				) +
				table(
					__("Why reviews failed in the last 24 hours"),
					[
						["count", __("Reviews")],
						["cause", __("Error")],
					],
					data.failures,
				) +
				table(
					__("AI gateway"),
					[
						["failures_in_a_row", __("Failures in a row")],
						["paused_for", __("Paused for (seconds)")],
						["outage_since", __("Outage since")],
					],
					[gateway],
				) +
				table(
					__("Indexes that need attention"),
					[
						["full_name", __("Repository")],
						["status", __("Status")],
						["files_processed", __("Files done")],
						["num_files", __("Files")],
						["started_at", __("Started")],
						["error", __("Error")],
					],
					data.indexes,
				),
		);
	};

	const load = () => frappe.xcall("guard.api.health.status").then(render);
	page.set_primary_action(__("Refresh"), load, "refresh");
	load();
};
