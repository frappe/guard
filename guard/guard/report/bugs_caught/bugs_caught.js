frappe.query_reports["Bugs Caught"] = {
	filters: [
		{ fieldname: "search", label: __("Search"), fieldtype: "Data" },
		{ fieldname: "severity", label: __("Severity"), fieldtype: "Select", options: ["", "P0", "P1", "P2"] },
		{ fieldname: "security", label: __("Security"), fieldtype: "Select", options: ["", "Yes", "No"] },
		{ fieldname: "status", label: __("Status"), fieldtype: "Select", options: ["", "open", "addressed"] },
		{ fieldname: "repository", label: __("Repository"), fieldtype: "Link", options: "Code Repository" },
		{ fieldname: "author", label: __("Author"), fieldtype: "Data" },
		{ fieldname: "period", label: __("Period"), fieldtype: "Select", options: ["all time", "this week", "this month", "this quarter", "this year"], default: "all time" },
	],
};
