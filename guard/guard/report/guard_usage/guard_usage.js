frappe.query_reports["Guard Usage"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -29),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: ["Day", "Tier", "Repository"],
			default: "Day",
		},
		{
			fieldname: "tier",
			label: __("Tier"),
			fieldtype: "Select",
			options: ["", "base", "plus", "apex"],
		},
		{
			fieldname: "repository",
			label: __("Repository"),
			fieldtype: "Link",
			options: "Code Repository",
		},
	],
};
