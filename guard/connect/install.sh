#!/bin/sh
# Connect the coding agents on this computer to Guard at __GUARD_SITE__:
# the MCP server, the Guard skills and, if you want, a pre-push hook. Run it again to update.
#
#   curl -fsSL __GUARD_SITE__/mcp/install.sh | sh
#   curl -fsSL __GUARD_SITE__/mcp/install.sh | sh -s -- --client claude --hook
#
# Options:
#   --hook          also stop pushes with P0 findings in the git repository of this folder
#   --no-hook       do not ask about the hook
#   --client NAME   install only claude, codex or pi (repeat for more); default: choose
#   --no-login      do not start the sign-in
#   --dry-run       show what would change, and change nothing
#   --uninstall     remove what this script installed
#
# Everything is in functions, and `main` is the last line. So `curl | sh` runs nothing before the
# whole script is downloaded, and a command that reads stdin cannot eat the rest of the script.

set -eu

SITE="__GUARD_SITE__"
MCP_URL="$SITE/mcp"
NAME=guard
SKILLS="guard-review guard-fix guard-feedback guard-ask"
HOOK_MARK="Installed by Guard"
DESCRIPTION="Guard, the code reviewer of the team: reviews before a push, findings of pull requests, code search in all repositories"

hook=ask
login=yes
dry=no
uninstall=no
clients=""
selected=no
all=no
kit=""
# Sign-in commands of the clients that this run set up, one on each line.
logins=""
agents_skills_done=no

say() { printf '%s\n' "$*"; }
step() { printf '  %s\n' "$*"; }
die() {
	printf 'guard: %s\n' "$*" >&2
	exit 1
}
has() { command -v "$1" >/dev/null 2>&1; }
# Run a command, or show it in a dry run. Its output is not shown, and it reads no stdin.
run() {
	if [ "$dry" = yes ]; then
		step "would run: $*"
	else
		"$@" </dev/null >/dev/null
	fi
}
# True when the script can ask questions. With `curl | sh`, the script is on stdin, so ask on the terminal.
interactive() { [ "$dry" = no ] && (exec </dev/tty) 2>/dev/null; }
ask() {
	interactive || return 1
	printf '%s [y/N] ' "$1" >/dev/tty
	read -r answer </dev/tty || answer=""
	case $answer in [Yy]*) return 0 ;; *) return 1 ;; esac
}
wanted() {
	has "$1" || return 1
	case " $clients " in *" $1 "*) return 0 ;; esac
	return 1
}

choose_clients() {
	available=""
	for client in claude codex pi; do
		has "$client" && available="$available $client"
	done
	for client in $clients; do
		has "$client" || die "$client is not installed."
	done
	# --all means the agents that are installed.
	if [ "$all" = yes ]; then clients="$clients $available"; fi
	[ "$selected" = no ] || return 0
	[ -n "$available" ] || die "found no supported coding agent (claude, codex or pi). Use --client NAME or the prompt on $SITE/desk/guard-agent."
	if ! interactive; then
		die "no terminal prompt is available. Choose one or more with --client claude, --client codex or --client pi."
	fi
	clients=""
	say "Choose which coding agents to set up (press Enter for none):"
	index=0
	choices=""
	for client in $available; do
		index=$((index + 1))
		choices="$choices $index:$client"
		step "$index) $client"
	done
	printf 'Numbers separated by spaces: ' >/dev/tty
	read -r answer </dev/tty || answer=""
	for number in $answer; do
		case $number in *[!0-9]* | "") die "choose the numbers shown above." ;; esac
		case " $choices " in
		*" $number:"*)
			client=${choices#*" $number:"}
			client=${client%% *}
			case " $clients " in *" $client "*) ;; *) clients="$clients $client" ;; esac
			;;
		*) die "choose one of the numbers shown above." ;;
		esac
	done
	selected=yes
	[ -n "$clients" ] || die "no agents selected. Run again and choose at least one."
}

usage() {
	cat <<EOF
Connect the coding agents on this computer to Guard at $SITE.

  curl -fsSL $SITE/mcp/install.sh | sh -s -- [options]

  --hook          also stop pushes with P0 findings in the git repository of this folder
  --no-hook       do not ask about the hook
  --client NAME   install only claude, codex or pi (repeat for more); default: choose
  --all           install every agent that is installed, without asking
  --no-login      do not start the sign-in
  --dry-run       show what would change, and change nothing
  --uninstall     remove what this script installed
EOF
}

parse() {
	while [ $# -gt 0 ]; do
		case $1 in
		--hook) hook=yes ;;
		--no-hook) hook=no ;;
		--no-login) login=no ;;
		--dry-run) dry=yes ;;
		--uninstall) uninstall=yes ;;
		--client)
			[ $# -gt 1 ] || die "--client needs claude, codex or pi."
			case $2 in
			claude | codex | pi)
				case " $clients " in *" $2 "*) ;; *) clients="$clients $2" ;; esac
				selected=yes
				;;
			*) die "unknown client $2. Use claude, codex or pi." ;;
			esac
			shift
			;;
		--all)
			all=yes
			selected=yes
			;;
		-h | --help)
			usage
			exit 0
			;;
		*) die "unknown option $1. Use --help." ;;
		esac
		shift
	done
}

fetch_kit() {
	has curl || die "curl is not installed."
	has tar || die "tar is not installed."
	kit=$(mktemp -d)
	trap 'rm -rf "$kit"' EXIT
	curl -fsSL "$SITE/mcp/kit.tar.gz" | tar -xzf - -C "$kit" || die "could not download the skills from $SITE."
	for skill in $SKILLS; do
		[ -f "$kit/skills/$skill/SKILL.md" ] || die "the download from $SITE has no skill $skill."
	done
}

# Replace the Guard skills in a folder. Other skills stay.
install_skills() {
	run mkdir -p "$1"
	for skill in $SKILLS; do
		run rm -rf "${1:?}/$skill"
		run cp -R "$kit/skills/$skill" "$1/$skill"
	done
	step "skills: $(echo "$SKILLS" | sed 's/ /, /g') in $1"
}

remove_skills() {
	removed=no
	for skill in $SKILLS; do
		if [ -d "$1/$skill" ]; then
			run rm -rf "${1:?}/$skill"
			removed=yes
		fi
	done
	if [ "$removed" = yes ]; then step "removed the Guard skills from $1"; fi
}

# A failed client does not stop the others.
failed() { step "failed: $*. Set it up by hand with the prompt on $SITE/desk/guard-agent."; }

setup_claude() {
	say "Claude Code"
	if claude mcp get "$NAME" </dev/null 2>/dev/null | grep -F "$MCP_URL" >/dev/null; then
		step "MCP server $NAME: already set up"
	else
		# Another address under the same name: replace it.
		if [ "$dry" = no ]; then claude mcp remove "$NAME" --scope user </dev/null >/dev/null 2>&1 || :; fi
		if run claude mcp add --transport http --scope user "$NAME" "$MCP_URL"; then
			step "MCP server $NAME: $MCP_URL"
		else
			failed "claude mcp add"
			return 0
		fi
	fi
	install_skills "$HOME/.claude/skills"
	logins="$logins
claude mcp login $NAME"
}

codex_config() { echo "${CODEX_HOME:-$HOME/.codex}/config.toml"; }

setup_codex() {
	say "Codex"
	config=$(codex_config)
	# The table can be written [mcp_servers.guard] or [mcp_servers."guard"].
	if [ -f "$config" ] && grep -Eq "^[[:space:]]*\[mcp_servers\.\"?$NAME\"?\]" "$config"; then
		if grep -F "\"$MCP_URL\"" "$config" >/dev/null; then
			step "MCP server $NAME: already set up"
		else
			step "MCP server $NAME: $config has another address. Run: codex mcp remove $NAME, then this script again."
		fi
	elif [ "$dry" = yes ]; then
		step "would add [mcp_servers.$NAME] to $config"
	else
		# Codex starts a sign-in in `codex mcp add`, so write the table. A new table at the end is valid TOML.
		mkdir -p "$(dirname "$config")"
		printf '\n[mcp_servers.%s]\nurl = "%s"\n' "$NAME" "$MCP_URL" >>"$config"
		step "MCP server $NAME: $MCP_URL in $config"
	fi
	# Codex and pi both read ~/.agents/skills.
	if [ "$agents_skills_done" = no ]; then
		install_skills "$HOME/.agents/skills"
		agents_skills_done=yes
	fi
	logins="$logins
codex mcp login $NAME"
}

setup_pi() {
	say "pi"
	# `pi mcp add` replaces the entry and keeps the sign-in. Direct exposure: the model sees the tools.
	if run pi mcp add "$NAME" --url "$MCP_URL" --exposure direct --description "$DESCRIPTION"; then
		step "MCP server $NAME: $MCP_URL"
	else
		failed "pi mcp add"
		return 0
	fi
	if [ "$agents_skills_done" = no ]; then
		install_skills "$HOME/.agents/skills"
		agents_skills_done=yes
	fi
	logins="$logins
pi mcp login $NAME"
}

hook_file() { echo "$(git rev-parse --git-path hooks)/pre-push"; }

install_hook() {
	if ! git rev-parse --git-dir >/dev/null 2>&1; then
		step "this folder is not in a git repository, so there is no hook. Run the script with --hook in one."
		return 0
	fi
	file=$(hook_file)
	if [ -f "$file" ] && ! grep -q "$HOOK_MARK" "$file"; then
		step "$file is another hook. Guard did not replace it."
		return 0
	fi
	run mkdir -p "$(dirname "$file")"
	run cp "$kit/pre-push" "$file"
	run chmod +x "$file"
	step "pre-push hook: $file. A P0 finding stops the push. Skip it once with: GUARD_SKIP=1 git push"
}

remove_hook() {
	git rev-parse --git-dir >/dev/null 2>&1 || return 0
	file=$(hook_file)
	if [ -f "$file" ] && grep -q "$HOOK_MARK" "$file"; then
		run rm -f "$file"
		step "removed the pre-push hook: $file"
	fi
}

uninstall_all() {
	if wanted claude; then
		say "Claude Code"
		run claude mcp remove "$NAME" --scope user 2>/dev/null || :
		remove_skills "$HOME/.claude/skills"
	fi
	if wanted codex; then
		say "Codex"
		run codex mcp remove "$NAME" 2>/dev/null || :
	fi
	if wanted pi; then
		say "pi"
		run pi mcp remove "$NAME" 2>/dev/null || :
	fi
	remove_skills "$HOME/.agents/skills"
	remove_hook
	say "Guard is removed."
	say "A hook stays in each other repository where you installed one. Run this in each of them:"
	step "curl -fsSL $SITE/mcp/install.sh | sh -s -- --uninstall"
}

sign_in() {
	if [ "$login" = yes ] && interactive; then
		say ""
		say "Sign in to Guard once in each agent. A browser opens."
		echo "$logins" | while read -r command; do
			[ -n "$command" ] || continue
			say "> $command"
			# shellcheck disable=SC2086 # the command is a list of words
			$command </dev/tty || step "the sign-in did not finish. Run later: $command"
		done
	else
		say ""
		say "Next, sign in to Guard once in each agent:"
		echo "$logins" | while read -r command; do [ -z "$command" ] || step "$command"; done
	fi
}

main() {
	parse "$@"
	if [ "$uninstall" = yes ]; then
		# Without a choice, remove Guard from every installed agent.
		if [ "$selected" = no ]; then all=yes selected=yes; fi
		choose_clients
		uninstall_all
		return 0
	fi
	choose_clients
	fetch_kit
	say "Guard: $SITE"
	if wanted claude; then setup_claude; fi
	if wanted codex; then setup_codex; fi
	if wanted pi; then setup_pi; fi

	if [ "$hook" = ask ] && git rev-parse --git-dir >/dev/null 2>&1 &&
		ask "Stop pushes with P0 findings in $(basename "$(git rev-parse --show-toplevel)")? Your agent reviews each push."; then
		hook=yes
	fi
	if [ "$hook" = yes ]; then
		if [ -n "$clients" ]; then
			say "Hook"
			install_hook
		else
			step "no agent selected; skipping the pre-push hook."
		fi
	fi

	[ -n "$clients" ] || return 0
	sign_in
	say ""
	say "Then ask your agent: \"Review my change with Guard.\""
	if [ "$hook" != yes ] && [ -n "$clients" ]; then
		say "To stop pushes with P0 findings in a repository, run this in it:"
		first=$(printf '%s\n' "$clients" | awk '{print $1}')
		step "curl -fsSL $SITE/mcp/install.sh | sh -s -- --client $first --hook --no-login"
	fi
}

main "$@"
