frappe.listview_settings["Code Review"] = {
	onload(listview) {
		listview.page.add_actions_menu_item(__("Retry"), () => {
			const reviews = listview.get_checked_items(true);
			if (!reviews.length) return;
			frappe.xcall("guard.api.ui.retry_reviews", { reviews }).then((created) => {
				frappe.show_alert({
					message: __("{0} pull requests queued for a new review", [created.length]),
					indicator: created.length ? "green" : "orange",
				});
				listview.refresh();
			});
		});
	},
};
