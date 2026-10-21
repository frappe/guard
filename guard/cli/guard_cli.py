#!/usr/bin/env python3
"""guard: review branches and fix findings from the terminal. Standard library only."""

import argparse
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_SITE = ""
VERSION = "1.0.0"
HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "guard"
CREDENTIALS = HOME / "credentials"
SETTINGS = HOME / "settings.json"
REPOS = HOME / "repos.json"
SETTING_KEYS = {
	"color": "bool",
	"site": "str",
	"review.output": "auto|text|json",
	"review.layout": "comments|diff",
	"review.context": "int:0:60",
	"review.width": "int:40:240",
}
SECRET_NAMES = re.compile(
	r"(^|/)(\.env(\..*)?|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key|.*\.p12|.*credentials.*|.*secrets?\.(json|ya?ml|toml))$",
	re.I,
)
SECRET_CONTENT = re.compile(
	r"-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|xox[baprs]-"
)
MAX_FILE = 512 * 1024
EXIT = {"done": 0, "none": 1, "invalid": 2, "running": 3, "failed": 4, "cancelled": 5}
AGENTS = {"claude": ["claude"], "codex": ["codex"], "cursor": ["cursor-agent"]}


# ------------------------------------------------------------------ settings


def load_json(path: Path, default):
	try:
		return json.loads(path.read_text())
	except OSError, ValueError:
		return default


def save_json(path: Path, data, private: bool = False) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(data, indent=2))
	if private:
		path.chmod(0o600)


def setting(name: str, default: Any = None) -> Any:
	return load_json(SETTINGS, {}).get(name, default)


def site() -> str:
	value = (
		os.environ.get("GUARD_SITE")
		or setting("site")
		or load_json(CREDENTIALS, {}).get("site")
		or DEFAULT_SITE
	)
	if not value:
		die("No site. Run `guard settings set site https://your-site`.", EXIT["invalid"])
	return str(value).rstrip("/")


def token() -> str:
	value = os.environ.get("GUARD_API_KEY") or load_json(CREDENTIALS, {}).get("token")
	if not value:
		die("Not signed in. Run `guard login`.", EXIT["none"])
	return str(value)


# ----------------------------------------------------------------------- http


def api(method: str, path: str, params: dict | None = None, body: Any = None, auth: bool = True) -> Any:
	url = f"{site()}/api/method/{path}"
	if params:
		url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
	data = json.dumps(body).encode() if body is not None else None
	request = urllib.request.Request(url, data=data, method=method)
	request.add_header("Accept", "application/json")
	if data is not None:
		request.add_header("Content-Type", "application/json")
	if auth:
		request.add_header("Authorization", f"token {token()}")
	try:
		with urllib.request.urlopen(request, timeout=120) as response:
			return json.loads(response.read() or b"{}").get("message")
	except urllib.error.HTTPError as error:
		detail = error.read().decode("utf-8", "replace")
		try:
			detail = json.loads(detail).get("exception") or detail
		except ValueError:
			pass
		die(f"{error.code}: {str(detail)[:400]}", EXIT["failed"])
	except urllib.error.URLError as error:
		die(f"Cannot reach {site()}: {error.reason}", EXIT["failed"])


# ------------------------------------------------------------------------ git


def git(*args: str, check: bool = True) -> str:
	result = subprocess.run(["git", *args], capture_output=True, text=True)
	if check and result.returncode != 0:
		die(result.stderr.strip() or f"git {args[0]} failed", EXIT["invalid"])
	return result.stdout.strip()


def full_name() -> str:
	url = git("remote", "get-url", "origin", check=False)
	match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
	return match.group(1) if match else ""


def default_branch() -> str:
	ref = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
	return ref.rsplit("/", 1)[-1] if ref else "main"


def payload(base: str, include: list[str]) -> tuple[dict, list[str]]:
	remote_base = f"origin/{base}"
	base_ref = remote_base if git("rev-parse", "--verify", "--quiet", remote_base, check=False) else base
	merge_base = git("merge-base", base_ref, "HEAD")
	head = git("rev-parse", "HEAD")
	names = [
		line.split("\t")[-1]
		for line in git("diff", "--name-status", "--find-renames", merge_base, head).splitlines()
		if line
	]
	held = [n for n in names if (SECRET_NAMES.search(n) or _secret_content(head, n)) and n not in include]
	diff = (
		git(
			"diff",
			"--no-color",
			"--find-renames",
			merge_base,
			head,
			"--",
			*[n for n in names if n not in held],
		)
		if names
		else ""
	)
	files, base_files = {}, {}
	for name in names:
		if name in held:
			continue
		for target, sha in ((files, head), (base_files, merge_base)):
			text = git("show", f"{sha}:{name}", check=False)
			if text and len(text) < MAX_FILE:
				target[name] = text
	for path in git(
		"ls-files", "--", ".guard", "*/.guard/*", "guard.toml", "*/guard.toml", "CLAUDE.md", "AGENTS.md"
	).splitlines():
		if path not in files:
			text = git("show", f"{head}:{path}", check=False)
			if text:
				files[path] = text
	commits = [
		{"sha": c[:40], "subject": c[41:]}
		for c in git("log", "--format=%H %s", f"{merge_base}..{head}").splitlines()
		if c
	]
	return {
		"full_name": full_name(),
		"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
		"base_branch": base,
		"head_sha": head,
		"base_sha": merge_base,
		"diff": diff,
		"files": files,
		"base_files": base_files,
		"tree": git("ls-tree", "-r", "--name-only", head).splitlines(),
		"commits": commits,
		"author": git("config", "user.email", check=False),
		"title": commits[-1]["subject"] if commits else "",
	}, held


def _secret_content(sha: str, name: str) -> bool:
	text = git("show", f"{sha}:{name}", check=False)
	return bool(text and SECRET_CONTENT.search(text[:MAX_FILE]))


# -------------------------------------------------------------------- output


def color(text: str, code: str, args) -> str:
	enabled = (
		args.color if getattr(args, "color", None) is not None else setting("color", sys.stdout.isatty())
	)
	return f"\033[{code}m{text}\033[0m" if enabled else text


SEVERITY_COLOR = {"P0": "31;1", "P1": "33;1", "P2": "36"}


def print_review(review: dict, args) -> None:
	mode = "json" if args.json else "text" if args.text else setting("review.output", "auto")
	if mode == "json" or (mode == "auto" and not sys.stdout.isatty() and args.json):
		print(json.dumps(review, indent=2))
		return
	width = args.width or int(setting("review.width", min(shutil.get_terminal_size((100, 20)).columns, 120)))
	context = args.context if args.context is not None else int(setting("review.context", 15))
	layout = "diff" if args.diff else args.layout or setting("review.layout", "comments")
	print(
		color(
			f"Review {review['id']} · {review['status']} · {review.get('confidence_score')}/5 · risk {review.get('risk_level') or '-'}",
			"1",
			args,
		)
	)
	if review.get("summary"):
		print("\n" + _wrap(review["summary"], width))
	findings = review.get("findings") or []
	if not findings:
		print("\nNo findings.")
	for finding in findings:
		head = f"{finding['severity']} {finding['comment_type']} {finding['path']}:{finding['start_line']}"
		print(
			"\n"
			+ color(head, SEVERITY_COLOR.get(finding["severity"], "0"), args)
			+ "  "
			+ color(finding["title"], "1", args)
		)
		if layout == "diff":
			print(_excerpt(finding, context))
		print(_wrap(finding.get("body") or "", width))
		if finding.get("suggestion"):
			print(color("suggestion:", "32", args))
			print("\n".join("  + " + line for line in finding["suggestion"].splitlines()))


def _excerpt(finding: dict, context: int) -> str:
	path = Path(finding["path"])
	if not path.exists():
		return ""
	lines = path.read_text(errors="replace").splitlines()
	start, end = max(finding["start_line"] - context, 1), min(finding["end_line"] + context, len(lines))
	marks = range(finding["start_line"], finding["end_line"] + 1)
	return "\n".join(f"{'>' if n in marks else ' '}{n:5} {lines[n - 1]}" for n in range(start, end + 1))


def _wrap(text: str, width: int) -> str:
	import textwrap

	return "\n".join(
		textwrap.fill(p, width) if not p.startswith(("```", "    ", "|", "-")) else p
		for p in text.splitlines()
	)


# ------------------------------------------------------------------- commands


def cmd_login(options) -> None:
	if options.site:
		save_json(SETTINGS, {**load_json(SETTINGS, {}), "site": options.site.rstrip("/")})
	if options.api_key:
		value = (
			sys.stdin.readline().strip()
			if not sys.stdin.isatty()
			else input("API key (key:secret): ").strip()
		)
		if ":" not in value:
			die("Use key:secret.", EXIT["invalid"])
		save_json(CREDENTIALS, {"site": site(), "token": value}, private=True)
		print("Signed in.")
		return
	result: dict = {}

	class Handler(http.server.BaseHTTPRequestHandler):
		def do_GET(self):
			query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
			result.update({k: v[0] for k, v in query.items()})
			self.send_response(200)
			self.end_headers()
			self.wfile.write(b"Guard CLI is signed in. You can close this tab.")

		def log_message(self, format: str, *args: Any) -> None:
			pass

	server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
	port = server.server_address[1]
	code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
	thread = threading.Thread(target=server.handle_request, daemon=True)
	thread.start()
	url = f"{site()}/desk/guard-cli-login?port={port}&code={code}"
	print(f"Opening {url}")
	print(f"Check that the page shows the code {code}.")
	webbrowser.open(url)
	thread.join(timeout=300)
	if not result.get("key"):
		die("Sign-in timed out.", EXIT["failed"])
	if result.get("code") != code:
		die("The sign-in code does not match. Nothing was saved.", EXIT["failed"])
	save_json(CREDENTIALS, {"site": site(), "token": f"{result['key']}:{result['secret']}"}, private=True)
	print("Signed in.")


def cmd_logout(options) -> None:
	CREDENTIALS.unlink(missing_ok=True)
	print("Signed out.")


def cmd_whoami(options) -> None:
	print(json.dumps(api("GET", "guard.api.cli.whoami"), indent=2))


def cmd_review(options) -> None:
	if options.plus and options.apex:
		die("--plus and --apex cannot be combined.", EXIT["invalid"])
	if options.resume:
		rows = api("GET", "guard.api.cli.recent", {"limit": 1, "full_name": full_name()}) or []
		if not rows or rows[0]["status"] not in ("Queued", "Running"):
			die("No unfinished review.", EXIT["none"])
		return _wait_and_print(rows[0]["name"], options)
	data, held = payload(options.branch or default_branch(), options.include or [])
	if held:
		print("Held back, they look like secrets (use --include to send):", ", ".join(held), file=sys.stderr)
	if not data["diff"].strip():
		die("No committed changes against the base branch.", EXIT["none"])
	tier = "apex" if options.apex else "plus" if options.plus else ""
	created = api(
		"POST",
		"guard.api.cli.review",
		body={"payload": data, "tier": tier, "instructions": options.instructions or ""},
	)
	_wait_and_print(created["id"], options)


def _wait_and_print(review_id: str, args) -> None:
	try:
		while True:
			review = api("GET", "guard.api.cli.show", {"review_id": review_id})
			if review["status"] not in ("Queued", "Running"):
				break
			if sys.stderr.isatty():
				print(f"\r{review['status']}: {review.get('stage') or ''}   ", end="", file=sys.stderr)
			time.sleep(3)
	except KeyboardInterrupt:
		sys.exit(130)
	if sys.stderr.isatty():
		print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
	print_review(review, args)
	if review["status"] == "Failed":
		sys.exit(EXIT["failed"])


def cmd_show(options) -> None:
	if options.id:
		return print_review(api("GET", "guard.api.cli.show", {"review_id": options.id}), options)
	rows = api("GET", "guard.api.cli.recent", {"limit": 10}) or []
	if options.json:
		print(json.dumps(rows, indent=2))
		return
	for row in rows:
		print(
			f"{row['name']}  {row['status']:<10} {row.get('branch') or '':<30} {str(row.get('head_sha') or '')[:8]}  {row['creation'][:16]}"
		)


def cmd_status(options) -> None:
	sha = git("rev-parse", options.commit or "HEAD")
	result = api("GET", "guard.api.cli.status", {"head_sha": sha, "full_name": full_name()})
	print(json.dumps(result) if options.json else (result.get("status") or "no review"))
	sys.exit(result["exit_code"])


def cmd_config(options) -> None:
	result = api("GET", "guard.api.cli.config", {"full_name": full_name(), "path": options.path or ""})
	if options.json:
		print(json.dumps(result, indent=2))
		return
	for key, item in result["settings"].items():
		print(f"{key:24} {json.dumps(item['value']):30} ({item['source']})")
	for rule in result["rules"]:
		print(f"rule  {rule['rule']}  ({rule['source']})")
	for error in result["errors"]:
		print(f"error {error}")


def cmd_settings(options) -> None:
	data = load_json(SETTINGS, {})
	if options.action in (None, "list"):
		print(
			json.dumps(data, indent=2) if options.json else "\n".join(f"{k} = {v}" for k, v in data.items())
		)
	elif options.action == "get":
		print(data.get(options.key, ""))
	elif options.action == "path":
		print(SETTINGS)
	elif options.action == "unset":
		data.pop(options.key, None)
		save_json(SETTINGS, data)
	elif options.action == "set":
		data[options.key] = _setting_value(options.key, options.value)
		save_json(SETTINGS, data)


def _setting_value(key: str, value: str):
	kind = SETTING_KEYS.get(key)
	if not kind:
		die(f"Unknown setting {key}. Known: {', '.join(SETTING_KEYS)}", EXIT["invalid"])
	if kind == "bool":
		return value.lower() in ("1", "true", "yes", "on")
	if kind.startswith("int:"):
		low, high = (int(x) for x in kind.split(":")[1:])
		number = int(value)
		if not low <= number <= high:
			die(f"{key} must be between {low} and {high}", EXIT["invalid"])
		return number
	if "|" in kind and value not in kind.split("|"):
		die(f"{key} must be one of {kind}", EXIT["invalid"])
	return value


def cmd_fix(options) -> None:
	if options.action == "install":
		_install_handler()
	elif options.action == "status":
		ok = _handler_installed()
		print(json.dumps({"installed": ok}) if options.json else ("ready" if ok else "not installed"))
	elif options.action == "uninstall":
		_uninstall_handler()
		if options.remove_mappings:
			REPOS.unlink(missing_ok=True)
	elif options.action == "open":
		_open_fix(options.url)


def _open_fix(url: str) -> None:
	parsed = urllib.parse.urlparse(url)
	query = urllib.parse.parse_qs(parsed.query)
	target = (query.get("target") or [parsed.path.strip("/")])[0]
	agent = (query.get("agent") or ["claude"])[0]
	result = api("GET", "guard.api.cli.fix_prompt", {"target": target})
	repos = load_json(REPOS, {})
	folder = repos.get(result["repository"])
	if not folder or not Path(folder).exists():
		folder = input(f"Local folder of {result['repository']}: ").strip()
		repos[result["repository"]] = str(Path(folder).expanduser().resolve())
		save_json(REPOS, repos)
		folder = repos[result["repository"]]
	command = AGENTS.get(agent, [agent])
	os.chdir(folder)
	os.execvp(command[0], [*command, result["prompt"]])


def _install_handler() -> None:
	executable = shutil.which("guard") or str(Path(sys.argv[0]).resolve())
	if sys.platform == "darwin":
		app = Path.home() / "Applications" / "Guard Fix.app"
		script = f'on open location u\n do shell script "{executable} fix open " & quoted form of u\nend open location'
		subprocess.run(["osacompile", "-o", str(app), "-e", script], check=True)
		plist = app / "Contents" / "Info.plist"
		subprocess.run(
			[
				"plutil",
				"-insert",
				"CFBundleURLTypes",
				"-json",
				'[{"CFBundleURLSchemes":["guard-fix"]}]',
				str(plist),
			],
			check=False,
		)
	else:
		desktop = Path.home() / ".local/share/applications/guard-fix.desktop"
		desktop.parent.mkdir(parents=True, exist_ok=True)
		terminal = os.environ.get("TERMINAL") or "x-terminal-emulator"
		desktop.write_text(
			f"[Desktop Entry]\nName=Guard Fix\nExec={terminal} -e {executable} fix open %u\nType=Application\nNoDisplay=true\nMimeType=x-scheme-handler/guard-fix;\n"
		)
		subprocess.run(
			["xdg-mime", "default", "guard-fix.desktop", "x-scheme-handler/guard-fix"], check=False
		)
	print("Installed the guard-fix:// handler.")


def _handler_installed() -> bool:
	if sys.platform == "darwin":
		return (Path.home() / "Applications" / "Guard Fix.app").exists()
	return (Path.home() / ".local/share/applications/guard-fix.desktop").exists()


def _uninstall_handler() -> None:
	if sys.platform == "darwin":
		shutil.rmtree(Path.home() / "Applications" / "Guard Fix.app", ignore_errors=True)
	else:
		(Path.home() / ".local/share/applications/guard-fix.desktop").unlink(missing_ok=True)
	print("Removed the guard-fix:// handler.")


def cmd_update(options) -> None:
	target = Path(sys.argv[0]).resolve()
	with urllib.request.urlopen(f"{site()}/api/method/guard.api.cli.download", timeout=60) as response:
		target.write_bytes(response.read())
	print(f"Updated {target}")


# ----------------------------------------------------------------------- main


def die(message: str, code: int) -> NoReturn:
	print(f"guard: {message}", file=sys.stderr)
	sys.exit(code)


def output_flags(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--json", action="store_true")
	parser.add_argument("--text", "--agent", action="store_true", dest="text")
	parser.add_argument("--layout", choices=["comments", "diff"])
	parser.add_argument("--diff", action="store_true")
	parser.add_argument("--context", type=int, choices=range(0, 61), metavar="0-60")
	parser.add_argument("--width", type=int, choices=range(40, 241), metavar="40-240")
	parser.add_argument("--color", action="store_true", default=None)
	parser.add_argument("--no-color", action="store_false", dest="color")


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="guard", description="AI code review from the terminal.")
	parser.add_argument("--version", action="version", version=VERSION)
	commands = parser.add_subparsers(dest="command", required=True)
	login = commands.add_parser("login")
	login.add_argument("--api-key", action="store_true")
	login.add_argument("--site")
	login.set_defaults(func=cmd_login)
	commands.add_parser("logout").set_defaults(func=cmd_logout)
	commands.add_parser("whoami").set_defaults(func=cmd_whoami)
	review = commands.add_parser("review")
	review_sub = review.add_subparsers(dest="sub")
	review.add_argument("-b", "--branch")
	review.add_argument("--resume", action="store_true")
	review.add_argument("--include", nargs="+")
	review.add_argument("--instructions")
	review.add_argument("--plus", action="store_true")
	review.add_argument("--apex", action="store_true")
	output_flags(review)
	review.set_defaults(func=cmd_review)
	show = review_sub.add_parser("show")
	show.add_argument("id", nargs="?")
	output_flags(show)
	show.set_defaults(func=cmd_show)
	status = review_sub.add_parser("status")
	status.add_argument("--commit")
	status.add_argument("--json", action="store_true")
	status.set_defaults(func=cmd_status)
	config = commands.add_parser("config")
	config.add_argument("path", nargs="?")
	config.add_argument("--json", action="store_true")
	config.set_defaults(func=cmd_config)
	settings = commands.add_parser("settings")
	settings.add_argument("action", nargs="?", choices=["list", "get", "set", "unset", "path"])
	settings.add_argument("key", nargs="?")
	settings.add_argument("value", nargs="?")
	settings.add_argument("--json", action="store_true")
	settings.set_defaults(func=cmd_settings)
	fix = commands.add_parser("fix")
	fix.add_argument("action", choices=["install", "status", "uninstall", "open"])
	fix.add_argument("url", nargs="?")
	fix.add_argument("--json", action="store_true")
	fix.add_argument("--remove-mappings", action="store_true")
	fix.set_defaults(func=cmd_fix)
	commands.add_parser("update").set_defaults(func=cmd_update)
	return parser


def main(argv: list[str] | None = None) -> None:
	args = build_parser().parse_args(argv)
	args.func(args)


if __name__ == "__main__":
	main()
