frappe.query_reports["Guard Analytics"] = {
	filters: [
		{
			fieldname: "period",
			label: __("Period"),
			fieldtype: "Select",
			options: [
				"this week",
				"this month",
				"this quarter",
				"this year",
				"all time",
				"custom",
			],
			default: "this month",
		},
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			depends_on: "eval:doc.period=='custom'",
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			depends_on: "eval:doc.period=='custom'",
		},
		{
			fieldname: "metric",
			label: __("Metric"),
			fieldtype: "Select",
			options: ["reviews", "bugs", "merge_time", "ratings", "contributors"],
			default: "reviews",
		},
		{
			fieldname: "bucket",
			label: __("Bucket"),
			fieldtype: "Select",
			options: ["day", "week", "month"],
			default: "day",
		},
		{ fieldname: "team", label: __("Team"), fieldtype: "Link", options: "Guard Team" },
		{
			fieldname: "repository",
			label: __("Repository"),
			fieldtype: "Link",
			options: "Code Repository",
		},
		{ fieldname: "author", label: __("Author"), fieldtype: "Data" },
	],
};
