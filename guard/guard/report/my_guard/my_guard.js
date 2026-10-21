frappe.query_reports["My Guard"] = {
	filters: [{ fieldname: "period", label: __("Period"), fieldtype: "Select", options: ["this month", "this quarter", "this year", "all time"], default: "this quarter" }],
};
