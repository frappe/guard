import frappe

from guard.utils import db

ROLES = ("Guard Admin", "Guard Member", "Guard Viewer")

# (doctype, index name, columns). Equality columns come first, then the range or sort column.
# A composite index also serves its leading column, so that column gets no `search_index` of its own.
COMPOSITE_INDEXES = (
	("Code Entity", "index_path", ("index", "path(255)")),
	("Code Entity", "index_entity_name", ("index", "entity_name")),
	("Code Entity", "index_qualified_name", ("index", "qualified_name(255)")),
	# Files, routes and routers of an index, and siblings in a folder.
	("Code Entity", "index_kind_path", ("index", "kind", "path(255)")),
	# Callees in line order, without a sort.
	("Code Edge", "index_source", ("index", "source_entity", "kind", "line")),
	("Code Edge", "index_target", ("index", "target_entity")),
	("Code Edge", "index_target_name", ("index", "target_name")),
	("Code Edge", "index_path", ("index", "path(255)")),
	# Inherits edges for the symbol table, and HTTP references for cross-repo routes.
	("Code Edge", "index_kind_target_name", ("index", "kind", "target_name")),
	("Code Chunk", "index_path", ("index", "path(255)")),
	("Code Chunk", "index_entity", ("index", "entity")),
	# Queued reviews to resume, running slots, monthly credits and review counts.
	("Code Review", "status_creation", ("status", "creation")),
	("Code Review", "repository_creation", ("repository", "creation")),
	("Review Finding", "pr_status", ("pull_request", "status")),
	# Noisy comment types of a repository in a time window.
	("Review Finding", "repository_creation", ("repository", "creation")),
	# Open findings number cards.
	("Review Finding", "status_posted", ("status", "posted")),
	("Pull Request", "repository_number", ("repository", "number")),
	# Open pull requests to poll, and merged ones in a time window.
	("Pull Request", "repository_state", ("repository", "state", "merged_at")),
	("Pull Request", "state_merged_at", ("state", "merged_at")),
	# Feature tips count the replies of one author in a repository.
	("Pull Request Comment", "repository_author", ("repository", "author")),
	("Knowledge Base Document", "repository_path", ("repository", "path")),
	# Deliveries whose job was lost.
	("Webhook Delivery", "status_creation", ("status", "creation")),
)


def before_install():
	create_roles()


def after_install():
	after_migrate()


def after_migrate():
	create_roles()
	create_organization_settings()
	create_indexes()
	from guard.indexing.vectors import VectorStore

	VectorStore.ensure_table()
	from guard.review.jobs import restart_dead_reviews

	restart_dead_reviews()


def create_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def create_organization_settings():
	if frappe.db.exists("Review Settings", "Organization"):
		return
	frappe.get_doc({"doctype": "Review Settings", "scope": "Organization"}).insert(ignore_permissions=True)


def create_indexes():
	for doctype, name, columns in COMPOSITE_INDEXES:
		table = f"tab{doctype}"
		current = _index_columns(table, name)
		if current == list(columns):
			continue
		# An index with other columns is replaced in the same statement.
		drop = f"DROP INDEX `{name}`, " if current else ""
		cols = ", ".join(_quote_column(column) for column in columns)
		# Names come from COMPOSITE_INDEXES, not from input.
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` {drop}ADD INDEX `{name}` ({cols})"
		)  # nosemgrep: frappe-sql-format-injection
	if not frappe.db.sql("SHOW INDEX FROM `tabCode Chunk` WHERE Key_name = 'content_fulltext'"):
		frappe.db.sql_ddl("ALTER TABLE `tabCode Chunk` ADD FULLTEXT INDEX `content_fulltext` (`content`)")


def _index_columns(table: str, name: str) -> list[str]:
	"""Columns of an index in order, as `column` or `column(prefix length)`."""
	# The table name comes from COMPOSITE_INDEXES, not from input.
	rows = db.dict_rows(
		f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", (name,)
	)  # nosemgrep: frappe-sql-format-injection
	rows.sort(key=lambda row: row["Seq_in_index"])
	return [
		f"{row['Column_name']}({row['Sub_part']})" if row["Sub_part"] else str(row["Column_name"])
		for row in rows
	]


def _quote_column(column: str) -> str:
	name, _, length = column.partition("(")
	return f"`{name}`({length}" if length else f"`{name}`"
