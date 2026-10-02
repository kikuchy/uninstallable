#!/usr/bin/env bash
# Bash 3.2+, jq 1.6+, mise, and standard Unix utilities. No Python runtime.
set -euo pipefail
export LC_ALL=C

say() { printf 'uninstallable: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
usage() {
    cat <<'EOF'
Usage: curl -fsSL URL | uninstallable.sh [options] [-- installer arguments]
  --name NAME          Override the detected app name
  --install-env VAR    Override the install-directory variable
  --layout root|bin    Variable points at the install root or bin directory
  --shell SHELL        Override shebang detection (sh, bash, dash, zsh)
  --dry-run            Print detection JSON without executing or registering
  -h, --help           Show this help
EOF
}

name= install_env= layout= shell= dry_run=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --name|--install-env|--layout|--shell)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die "Missing value for $1."
            case $1 in
                --name) name=$2 ;; --install-env) install_env=$2 ;;
                --layout) layout=$2 ;; --shell) shell=$2 ;;
            esac
            shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        --) shift; break ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1. Pass installer arguments after --." ;;
    esac
done
[[ -z $name || $name =~ ^[a-z][a-z0-9_-]{0,63}$ ]] || die 'Invalid --name.'
[[ -z $install_env || $install_env =~ ^[A-Z][A-Z0-9_]*$ ]] || die 'Invalid --install-env.'
case $layout in ''|root|bin) ;; *) die 'Invalid --layout.' ;; esac
case $shell in ''|sh|bash|dash|zsh) ;; *) die 'Invalid --shell.' ;; esac
[ ! -t 0 ] || die 'Pipe an installer into this command.'
command -v jq >/dev/null || die 'jq is required and must be on PATH.'

# Resolve existing paths without GNU realpath/readlink -f (absent on older macOS).
canonical() {
    local path=$1 parent target hops=0
    while :; do
        parent=$(cd -P -- "$(dirname -- "$path")" && pwd) || return
        path=$parent/$(basename -- "$path")
        [ -L "$path" ] || break
        hops=$((hops + 1)); [ "$hops" -le 40 ] || return 1
        target=$(readlink "$path") || return
        case $target in /*) path=$target ;; *) path=$parent/$target ;; esac
    done
    printf '%s\n' "$path"
}

work= lock= plugin= link= backup= tool=
locked=0 transaction=0 installed=0 activated=0 new_link=0
cleanup() {
    local status=$? keep=0
    trap - EXIT INT TERM
    set +e
    if [ "$transaction" = 1 ] && [ "$activated" = 0 ]; then
        if [ "$installed" = 1 ]; then
            if ! mise uninstall "$tool" >&2; then
                say "Cleanup failed; keeping plugin for recovery: $plugin"
                keep=1
            fi
        fi
        if [ "$keep" = 0 ]; then
            if [ "$new_link" = 1 ] && [ -L "$link" ] && [ "$(readlink "$link")" = "$plugin" ]; then
                rm -- "$link"
            fi
            [ ! -d "$plugin" ] || rm -rf -- "$plugin"
            [ -z "$backup" ] || mv -- "$backup" "$plugin"
        fi
    fi
    [ "$locked" = 0 ] || rmdir -- "$lock"
    [ -z "$work" ] || rm -rf -- "$work"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
work=$(mktemp -d "${TMPDIR:-/tmp}/uninstallable.XXXXXXXX")
work=$(cd -- "$work" && pwd -P)
script=$work/installer.sh
# Bound input even when the producer sends unbounded data. Never execute a stream.
head -c 8388609 > "$script"
size=$(wc -c < "$script")
[ "$size" -le 8388608 ] || die 'Oversized installer (limit: 8 MiB).'
[ "$size" -gt 0 ] && grep -q '[^[:space:]]' "$script" || die 'Empty installer.'
non_nul=$(tr -d '\000' < "$script" | wc -c)
[ "$size" -eq "$non_nul" ] || die 'Binary installer.'
iconv -f UTF-8 -t UTF-8 "$script" >/dev/null 2>&1 || die 'Installer must be UTF-8 text.'

if [ -z "$shell" ]; then
    IFS= read -r first < "$script" || :
    if [[ $first == '#!'* ]]; then
        words=()
        read -r -a words <<< "${first#\#!}"
        if [ "${words[0]##*/}" = env ]; then words=("${words[@]:1}"); fi
        [ "${#words[@]}" -eq 1 ] || die 'Unsupported shebang; specify --shell explicitly.'
        shell=${words[0]##*/}
    else
        shell=sh
    fi
fi
case $shell in sh|bash|dash|zsh) ;; *) die 'Unsupported shebang; specify --shell explicitly.' ;; esac
command -v "$shell" >/dev/null || die "Required shell is unavailable: $shell"
"$shell" -n < "$script" || die 'Installer has invalid shell syntax.'

# jq performs text matching and JSON construction; vendor code is never evaluated.
plan=$work/plan.json
jq -Rs --arg name "$name" --arg variable "$install_env" --arg layout "$layout" '
    def suffix: "_(UNMANAGED_INSTALL|INSTALL_DIR|INSTALL_ROOT|INSTALL|PREFIX|HOME)$";
    def generic: IN("INSTALL_DIR", "INSTALL_ROOT", "INSTALL_PREFIX", "PREFIX", "BIN_DIR", "BINDIR");
    def word($v): test("(^|[^A-Za-z0-9_])" + $v + "([^A-Za-z0-9_]|$)");
    split("\n") | map(select(test("^\\s*#") | not)) | join("\n") as $code |
    ([$code | scan("\\$\\{([A-Z][A-Z0-9_]*)(?=[:}+?=-])") | .[0]] +
     [$code | split("\n")[] | match("^\\s*([a-z_][a-z0-9_]*)=[\u0027\"]?([A-Z][A-Z0-9_]*)[\u0027\"]?\\s*$"; "g") |
        .captures as $m | select($code | test("\\$\\{!" + $m[0].string + "(?=[:}])")) | $m[1].string]
    | unique | map(select(generic or test(suffix))) |
      map(select(IN("CARGO_HOME", "RUSTUP_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME") | not))) as $candidates |
    ($candidates | map(select(endswith("_UNMANAGED_INSTALL")))) as $unmanaged |
    (if $variable != "" then $variable
     elif ($unmanaged | length) == 1 then $unmanaged[0]
     elif ($candidates | length) == 1 then $candidates[0] else "" end) as $var |
    (if $name != "" then $name
     elif $var != "" and ($var | generic | not) and ($var | test(suffix)) then
        $var | sub(suffix; "") | ascii_downcase | gsub("_"; "-")
     else ([$code | split("\n")[] | scan("^\\s*(?:APP_NAME|BINARY_NAME|PROGRAM_NAME|app_name|binary_name)=[\u0027\"]?([a-z][a-z0-9_-]*)[\u0027\"]?\\s*$") | .[0]] | unique) as $names |
        if ($names | length) == 1 then $names[0] else "" end end) as $app |
    if $var == "" then {error: "Cannot identify one install-directory variable.", candidates: $candidates}
    elif ($code | word($var) | not) then {error: "The specified install variable does not occur in this script."}
    elif $app == "" then {error: "Cannot identify the app name; supply --name foo."}
    else {
        name: $app, install_env: $var,
        layout: (if $layout != "" then $layout
                 elif ($var | test("(_DIR|_UNMANAGED_INSTALL)$") or IN("BIN_DIR", "BINDIR")) then "bin" else "root" end),
        extra_env: ((["INSTALLER_NO_MODIFY_PATH", "NO_MODIFY_PATH", "UV_NO_MODIFY_PATH"] |
            map(. as $v | select($code | word($v)) | {key: $v, value: "1"}) | from_entries) +
            (if $var | endswith("_UNMANAGED_INSTALL") then
                {($var | sub("_UNMANAGED_INSTALL$"; "_INSTALL_DIR")): "", CARGO_DIST_FORCE_INSTALL_DIR: ""}
             elif $var == "BUN_INSTALL" then {SHELL: "/bin/sh"}
             elif $var == "DENO_INSTALL" then {CI: "1"} else {} end))
    } end
' "$script" > "$plan"

if jq -e 'has("error")' "$plan" >/dev/null; then
    reason=$(jq -r .error "$plan")
    [ "$dry_run" = 0 ] || die "$reason"
    say "$reason"
    say 'mise cannot manage this installer. A normal install cannot be removed with mise uninstall.'
    if { exec 3<> /dev/tty; } 2>/dev/null; then
        printf 'Run the original installer anyway? [y/N] ' >&3
        IFS= read -r answer <&3 || answer=
        exec 3>&-
        answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        case $answer in y|yes) ;; *) die 'Installation cancelled. Nothing was executed.' ;; esac
    else
        die 'No interactive terminal; installation cancelled. Nothing was executed.'
    fi
    say 'Running the original installer without mise management.'
    "$shell" -s -- "$@" < "$script"
    exit $?
fi
fields=$(jq -er '[.name, .install_env, .layout] | @tsv' "$plan")
IFS=$'\t' read -r name install_env layout <<< "$fields"
[[ $name =~ ^[a-z][a-z0-9_-]{0,63}$ ]] || die 'Invalid detected app name.'
jq -e --arg shell "$shell" --args '. + {shell: $shell, args: $ARGS.positional}' -- "$@" < "$plan" > "$work/recipe.json"
if [ "$dry_run" = 1 ]; then cat "$work/recipe.json"; exit 0; fi
command -v mise >/dev/null || die 'mise is required and must be on PATH.'

expand_home() { case $1 in '~') printf '%s\n' "$HOME" ;; '~/'*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;; *) printf '%s\n' "$1" ;; esac; }
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
state=$(expand_home "${UNINSTALLABLE_DATA_DIR:-$data_home/uninstallable}")
mise_data=$(expand_home "${MISE_DATA_DIR:-$data_home/mise}")
case $mise_data in /*) ;; *) mise_data=$PWD/$mise_data ;; esac
mkdir -p -- "$state"
state=$(cd -- "$state" && pwd -P)
lock=$state/.lock
mkdir -- "$lock" 2>/dev/null || die "Another install is running (or a stale lock remains): $lock"
locked=1
cd -- "$work"
plugin=$state/plugins/$name
link=$mise_data/plugins/$name
owned=0
[ ! -L "$plugin" ] || die "Refusing to replace a symlink: $plugin"
if [ -f "$plugin/.uninstallable" ] && [ "$(cat "$plugin/.uninstallable")" = uninstallable-v1 ]; then owned=1; fi
if [ -e "$plugin" ] && [ "$owned" = 0 ]; then die "Refusing to replace an existing directory: $plugin"; fi
if [ -e "$link" ] || [ -L "$link" ]; then
    [ "$owned" = 1 ] && [ -L "$link" ] && [ "$(canonical "$link")" = "$plugin" ] ||
        die "An existing mise plugin already owns $name. Use --name to choose another name."
else
    new_link=1
fi
mise ls --json > "$work/listing.json"
conflict=$(jq --arg name "$name" --argjson owned "$owned" '
    [to_entries[] | select((.key | split(":") | last) == $name) | .value[]] |
    any(.installed == true) or (length > 0 and $owned == 0)
' "$work/listing.json")
[ "$conflict" = false ] || die "mise already has a tool named $name. Refusing to replace it."
version=0.0.0-uninstallable
tool=$name@$version
if [ -d "$plugin" ]; then
    [ ! -e "$plugin.previous" ] && [ ! -L "$plugin.previous" ] || die "A previous recovery directory exists: $plugin.previous"
    mv -- "$plugin" "$plugin.previous"
    backup=$plugin.previous
fi
transaction=1
mkdir -p -- "$plugin/bin"
printf '%s' uninstallable-v1 > "$plugin/.uninstallable"
cp -- "$script" "$plugin/installer.sh"
jq --arg version "$version" '. + {version: $version}' "$work/recipe.json" > "$plugin/recipe.json"
{
    cat <<'EOF'
#!/bin/sh
set -eu
plugin_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ASDF_INSTALL_PATH/bin"
EOF
    printf 'env %s="${ASDF_INSTALL_PATH}' "$install_env"
    [ "$layout" != bin ] || printf '/bin'
    printf '" '
    jq -jr '.extra_env | to_entries | map(.key + "=" + .value) | @sh' "$plan"
    printf ' '
    # @sh escapes all values as shell literals, including quotes and newlines.
    jq -jr --argjson deno "$(if [ "$install_env" = DENO_INSTALL ] && grep -q -- '--no-modify-path' "$script"; then printf true; else printf false; fi)" '
        [.shell, "-s", "--"] + .args +
        (if $deno and (.args | index("--no-modify-path") | not) then ["--no-modify-path"] else [] end) | @sh
    ' "$work/recipe.json"
    printf ' < "$plugin_dir/installer.sh"\n'
} > "$plugin/bin/install"
printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$version" > "$plugin/bin/list-all"
printf '#!/bin/sh\nprintf "%%s\\n" "bin ."\n' > "$plugin/bin/list-bin-paths"
chmod +x "$plugin/bin/"*
if [ "$new_link" = 1 ]; then mise plugins link "$name" "$plugin" >&2; fi
say "Installing $name using $install_env ($layout layout)."
mise install "$tool" >&2
installed=1
destination=$(mise where "$tool")
destination=$(cd -- "$destination" && pwd -P)
found=0
shopt -s nullglob dotglob
for item in "$destination/"* "$destination/bin/"*; do
    [ -f "$item" ] && [ -x "$item" ] || continue
    resolved=$(canonical "$item") || die "Cannot resolve executable: $item"
    case $resolved in "$destination/"*) ;; *) die "An executable points outside the mise install directory: $item" ;; esac
    found=1
done
[ "$found" = 1 ] || die 'Installer produced no executables in the install root or bin/. Check --layout; external writes cannot be rolled back.'
mise use --global "$tool" >&2
activated=1
if [ -n "$backup" ]; then rm -rf -- "$backup"; fi
say "Installed $name. Remove with: mise uninstall $name"
