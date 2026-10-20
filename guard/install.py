import frappe

ROLES = ("Guard Admin", "Guard Member", "Guard Viewer")

# (doctype, index name, columns)
COMPOSITE_INDEXES = (
	("Code Entity", "index_path", ("index", "path(255)")),
	("Code Entity", "index_entity_name", ("index", "entity_name")),
	("Code Entity", "index_qualified_name", ("index", "qualified_name(255)")),
	("Code Edge", "index_source", ("index", "source_entity")),
	("Code Edge", "index_target", ("index", "target_entity")),
	("Code Edge", "index_target_name", ("index", "target_name")),
	("Code Edge", "index_path", ("index", "path(255)")),
	("Code Chunk", "index_path", ("index", "path(255)")),
	("Review Finding", "pr_status", ("pull_request", "status")),
	("Pull Request", "repository_number", ("repository", "number")),
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
		if frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", (name,)):
			continue
		cols = ", ".join(_quote_column(column) for column in columns)
		frappe.db.sql_ddl(f"ALTER TABLE `{table}` ADD INDEX `{name}` ({cols})")
	if not frappe.db.sql("SHOW INDEX FROM `tabCode Chunk` WHERE Key_name = 'content_fulltext'"):
		frappe.db.sql_ddl("ALTER TABLE `tabCode Chunk` ADD FULLTEXT INDEX `content_fulltext` (`content`)")


def _quote_column(column: str) -> str:
	name, _, length = column.partition("(")
	return f"`{name}`({length}" if length else f"`{name}`"
