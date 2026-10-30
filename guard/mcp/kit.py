"""The files that connect a coding agent to Guard: the install script, the skills and the pre-push hook.

`/mcp/install.sh` is the install script with the address of the site in it. `/mcp/kit.tar.gz` has the
skills and the hook. Both are public: they hold no secret, and the script runs before the sign-in.
The site serves them, so each developer gets the skills that match the server.
"""

import io
import tarfile
from pathlib import Path

import frappe

from guard.mcp.tools import SAFE_URL
from guard.settings import public_url

SITE_MARK = "__GUARD_SITE__"
EXECUTABLE = 0o755
FILE = 0o644


def folder() -> Path:
	return Path(frappe.get_app_path("guard", "connect"))


def site() -> str:
	address = public_url("")
	if not SAFE_URL.fullmatch(address):
		# The script and the hook put the address in shell commands, and fetch code from it.
		frappe.throw(
			frappe._("The Public URL in Guard Settings must be https, without characters that a shell reads.")
		)
	return address


def install_script() -> str:
	return (folder() / "install.sh").read_text().replace(SITE_MARK, site())


def archive() -> bytes:
	"""The skills and the hook, as the install script unpacks them. The hook sends diffs to this site."""
	address = site()
	root = folder()
	files = [root / "pre-push", *sorted(p for p in (root / "skills").rglob("*") if p.is_file())]
	buffer = io.BytesIO()
	# mtime 0 and no owner: the same files give the same bytes.
	with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
		for path in files:
			content = path.read_bytes()
			if path.name == "pre-push":
				content = content.replace(SITE_MARK.encode(), address.encode())
			info = tarfile.TarInfo(path.relative_to(root).as_posix())
			info.size, info.mtime = len(content), 0
			info.mode = EXECUTABLE if path.name == "pre-push" else FILE
			tar.addfile(info, io.BytesIO(content))
	return buffer.getvalue()


def install_command(*options: str) -> str:
	command = f"curl -fsSL {public_url('/mcp/install.sh')} | sh"
	return f"{command} -s -- {' '.join(options)}" if options else command
