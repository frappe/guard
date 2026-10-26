// Guard Health: how reviews ran over a period, with cards and charts, then queues, workers,
// failures, the AI gateway and the indexes.

frappe.pages["guard-health"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Guard Health"),
		single_column: true,
	});
	// Desk gives page.main no padding: line the content up with the page title.
	const $body = $(
		`<div style="padding: var(--padding-sm) var(--padding-md) var(--padding-lg)"></div>`
	).appendTo(page.main);
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	let charts = [];

	// Hours, and the label of the period. The server has the same hours in `metrics.PERIODS`.
	const periods = [
		["1", __("Last hour")],
		["6", __("Last 6 hours")],
		["24", __("Last 24 hours")],
		["168", __("Last 7 days")],
		["720", __("Last 30 days")],
	];
	const period = page.add_field({
		fieldname: "period",
		label: __("Period"),
		fieldtype: "Select",
		options: periods.map(([value, label]) => ({ value, label })),
		default: "24",
		change: () => load(),
	});

	// ------------------------------------------------------------ formats

	const none = "–";
	const num = (v, digits = 1) => (v == null ? none : format_number(v, null, digits));
	const minutes = (v) => (v == null ? none : __("{0} min", [num(v)]));
	const share = (v) => (v == null ? none : `${Math.round(v * 100)}%`);
	const usd = (v, digits = 2) => (v == null ? none : format_currency(v, "USD", digits));
	const compact = (v) => {
		if (!v) return "0";
		if (v >= 1e6) return `${num(v / 1e6)}M`;
		if (v >= 1e3) return `${num(v / 1e3)}K`;
		return String(v);
	};

	// ------------------------------------------------------------ blocks

	const heading = (title) => `<h5 class="mt-4 mb-3">${esc(title)}</h5>`;

	const table = (title, columns, rows) => {
		if (!rows.length)
			return `${heading(title)}<p class="text-muted">${__("Nothing to show.")}</p>`;
		const head = columns.map(([, label]) => `<th>${esc(label)}</th>`).join("");
		const body = rows
			.map(
				(row) =>
					`<tr>${columns
						.map(
							([key, , format]) =>
								`<td>${esc(format ? format(row[key]) : row[key])}</td>`
						)
						.join("")}</tr>`
			)
			.join("");
		return `${heading(title)}
			<table class="table table-bordered table-sm"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
	};

	const card = (label, value, note) => `
		<div style="border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
			padding: var(--padding-sm) var(--padding-md); background: var(--card-bg)">
			<div class="text-muted small">${esc(label)}</div>
			<div style="font-size: var(--text-2xl); font-weight: 600">${esc(value)}</div>
			<div class="text-muted small">${esc(note || "")}</div>
		</div>`;

	const grid = (min, html) =>
		`<div style="display: grid; gap: var(--margin-md);
			grid-template-columns: repeat(auto-fill, minmax(${min}px, 1fr))">${html}</div>`;

	// A wide chart takes the whole row, so that its labels fit.
	const chart_box = (key, title, wide = false) => `
		<div style="border: 1px solid var(--border-color); border-radius: var(--border-radius-md);
			padding: var(--padding-sm); ${wide ? "grid-column: 1 / -1" : ""}">
			<div class="small font-weight-bold" style="padding: 0 var(--padding-xs)">${esc(title)}</div>
			<div data-chart="${key}"></div>
		</div>`;

	// Millions when the busiest bucket reaches a million tokens, else thousands.
	const token_scale = (m) =>
		Math.max(0, ...m.series.map((row) => row.input_tokens)) >= 1e6
			? [1e6, __("Tokens (millions)")]
			: [1e3, __("Tokens (thousands)")];

	// Frappe 17 draws through `frappe.ui.chart`, and Frappe 16 has only `frappe.Chart`.
	const draw = (el, config) =>
		frappe.ui.chart ? frappe.ui.chart(el, config) : new frappe.Chart(el, config);

	// Empty buckets have no value. Bars, not lines: a bucket with no reviews then shows no bar,
	// and not a line that drops to 0.
	const values = (series, key, scale = 1) => series.map((row) => (row[key] || 0) / scale);

	const chart_configs = (m) => {
		const labels = m.series.map((row) => row.label);
		const base = { height: 220, axisOptions: { xIsSeries: 1 } };
		const bars = (datasets, colors) => ({
			...base,
			type: "bar",
			colors,
			data: { labels, datasets },
		});
		const set = (name, key, scale) => ({ name, values: values(m.series, key, scale) });
		return {
			throughput: {
				...base,
				type: "bar",
				barOptions: { stacked: 1 },
				colors: ["green", "red", "grey"],
				data: {
					labels,
					datasets: [
						set(__("Completed"), "completed"),
						set(__("Failed"), "failed"),
						set(__("Cancelled"), "cancelled"),
					],
				},
			},
			duration: bars(
				[set(__("Median"), "duration_p50"), set(__("90th percentile"), "duration_p90")],
				["blue", "orange"]
			),
			wait: bars(
				[set(__("Median"), "wait_p50"), set(__("90th percentile"), "wait_p90")],
				["blue", "orange"]
			),
			work: bars(
				[set(__("Turns"), "turns"), set(__("Rounds"), "rounds")],
				["purple", "cyan"]
			),
			speed: bars([set(__("Turns per minute"), "turns_per_minute")], ["green"]),
			tokens: {
				...base,
				type: "bar",
				colors: ["blue", "light-blue", "orange"],
				data: {
					labels,
					datasets: [
						set(__("Input"), "input_tokens", token_scale(m)[0]),
						set(__("Cached"), "cached_tokens", token_scale(m)[0]),
						set(__("Output"), "output_tokens", token_scale(m)[0]),
					],
				},
			},
			stages: m.stages.length && {
				height: 220,
				type: "bar",
				colors: ["purple"],
				data: {
					labels: m.stages.map((row) => row.stage),
					datasets: [
						{ name: __("Seconds"), values: m.stages.map((row) => row.seconds) },
					],
				},
			},
		};
	};

	// ------------------------------------------------------------ render

	const render_metrics = (m, label) => {
		const s = m.summary;
		const ended = s.completed + s.failed;
		const cards = [
			card(
				__("Throughput"),
				__("{0} / hour", [num(s.per_hour, 2)]),
				__("{0} completed", [s.completed])
			),
			card(
				__("In flight"),
				__("{0} running", [m.in_flight.running]),
				__("{0} queued", [m.in_flight.queued])
			),
			card(
				__("Review time"),
				minutes(s.duration_p50),
				__("median, 90th percentile {0}", [minutes(s.duration_p90)])
			),
			card(
				__("Wait to start"),
				minutes(s.wait_p50),
				__("median, 90th percentile {0}", [minutes(s.wait_p90)])
			),
			card(
				__("Turns per review"),
				num(s.turns),
				__("{0} rounds on average", [num(s.rounds)])
			),
			card(__("Turns per minute"), num(s.turns_per_minute, 2), __("in the review stage")),
			card(
				__("Failed"),
				share(ended ? s.failed / ended : null),
				__("{0} failed, {1} cancelled", [s.failed, s.cancelled])
			),
			card(__("Retried"), share(s.retried), __("of completed reviews")),
			card(
				__("Tokens"),
				compact(s.input_tokens + s.output_tokens),
				__("{0} cached", [share(s.cache_rate)])
			),
			card(__("Cost"), usd(s.cost), __("{0} per review", [usd(s.cost_per_review, 3)])),
			card(
				__("Findings per review"),
				num(s.findings),
				__("{0} posted on average", [num(s.posted)])
			),
		];
		const boxes = [
			chart_box("throughput", __("Reviews that ended")),
			chart_box("duration", __("Review time (minutes)")),
			chart_box("wait", __("Wait to start (minutes)")),
			chart_box("work", __("Turns and rounds per review")),
			chart_box("speed", __("Turns per minute")),
			chart_box("tokens", token_scale(m)[1]),
		];
		if (m.stages.length)
			boxes.push(
				chart_box("stages", __("Where the time goes (mean seconds per stage)"), true)
			);
		return (
			heading(__("Reviews: {0}", [label])) +
			grid(180, cards.join("")) +
			`<div class="mt-3">${grid(420, boxes.join(""))}</div>` +
			table(
				__("By tier"),
				[
					["tier", __("Tier")],
					["completed", __("Completed")],
					["failed", __("Failed")],
					["duration_p50", __("Median time"), minutes],
					["duration_p90", __("90th percentile"), minutes],
					["turns", __("Turns"), num],
					["rounds", __("Rounds"), num],
					["turns_per_minute", __("Turns per minute"), (v) => num(v, 2)],
					["cost_per_review", __("Cost per review"), (v) => usd(v, 3)],
				],
				m.tiers
			)
		);
	};

	const render = (data, label) => {
		const rate = data.failure_rate;
		const gateway = data.gateway;
		const alerts = [];
		if (gateway.paused_for)
			alerts.push(
				__("Reviews are paused: the AI gateway is down since {0}. Last error: {1}", [
					esc(gateway.outage_since),
					esc(gateway.last_error),
				])
			);
		if (rate.ended && rate.rate > 0.2)
			alerts.push(
				__("{0} of {1} reviews failed in the last hour.", [rate.failed, rate.ended])
			);
		const banner = alerts.length
			? `<div class="alert alert-danger">${alerts.join("<br>")}</div>`
			: `<div class="alert alert-success">${__("No problems found.")}</div>`;
		const reviews = Object.entries(data.reviews).map(([status, count]) => ({ status, count }));
		charts.forEach((chart) => chart.destroy?.());
		$body.html(
			banner +
				render_metrics(data.metrics, label) +
				table(
					__("Queues"),
					[
						["queue", __("Queue")],
						["queued", __("Waiting")],
						["running", __("Running")],
						["workers", __("Workers")],
						["error", __("Error")],
					],
					data.queues
				) +
				table(
					__("Reviews created: {0}", [label]),
					[
						["status", __("Status")],
						["count", __("Reviews")],
					],
					reviews
				) +
				table(
					__("Why reviews failed: {0}", [label]),
					[
						["count", __("Reviews")],
						["cause", __("Error")],
					],
					data.failures
				) +
				table(
					__("AI gateway"),
					[
						["failures_in_a_row", __("Failures in a row")],
						["paused_for", __("Paused for (seconds)")],
						["outage_since", __("Outage since")],
					],
					[gateway]
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
					data.indexes
				)
		);
		const configs = chart_configs(data.metrics);
		charts = Object.entries(configs)
			.filter(([, config]) => config)
			.map(([key, config]) => draw($body.find(`[data-chart="${key}"]`)[0], config));
	};

	const load = () => {
		const [hours, label] =
			periods.find(([value]) => value === period.get_value()) || periods[2];
		return frappe
			.xcall("guard.api.health.status", { hours: Number(hours) })
			.then((data) => render(data, label));
	};
	page.set_primary_action(__("Refresh"), load, "refresh-cw");
	load();
};
