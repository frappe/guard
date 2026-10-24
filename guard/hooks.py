app_name = "guard"
app_title = "Guard"
app_publisher = "Sabu Siyad"
app_description = "AI assisted code reviews and automatic pull request reviews"
app_email = "hello@ssiyad.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

export_python_type_annotations = True
require_type_annotated_api_methods = True

app_logo_url = "/assets/guard/guard.svg"

add_to_apps_screen = [
	{
		"name": "guard",
		"logo": "/assets/guard/guard.svg",
		"title": "Guard",
		"route": "/desk/guard",
	}
]

before_install = "guard.install.before_install"
after_install = "guard.install.after_install"
after_migrate = "guard.install.after_migrate"

scheduler_events = {
	"cron": {
		"* * * * *": ["guard.review.jobs.resume_waiting"],
		"*/5 * * * *": ["guard.webhooks.polling.poll", "guard.review.jobs.fail_dead_reviews"],
		"17 * * * *": ["guard.review.jobs.close_stuck_checks"],
		"*/15 * * * *": [
			"guard.learning.addressed.poll_reactions",
			"guard.api.health.check_failure_rate",
		],
		"0 */6 * * *": ["guard.knowledge.builder.sync_stale"],
	},
	"daily": [
		"guard.webhooks.endpoint.cleanup_deliveries",
		"guard.learning.suggestions.update_rule_metrics",
		"guard.learning.memories.retire_unhelpful",
	],
	"daily_long": ["guard.git.maintenance.run"],
	"weekly": ["guard.learning.suggestions.weekly_suggestions"],
}

has_permission = {
	"Custom Context": "guard.permissions.custom_context",
	"Review Settings": "guard.permissions.review_settings",
	"Code Repository": "guard.permissions.code_repository",
}

default_log_clearing_doctypes = {
	"Chat Session": 180,
	"Webhook Delivery": 30,
}
