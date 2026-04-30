#!/usr/bin/env bash
# magicwand installer.
#
# Intended usage from a WDL command block — the image must have ONE of
# curl, wget, or python3 to fetch this script:
#
#     MW_URL=https://raw.githubusercontent.com/broadinstitute/magicwand/main/install.sh
#     source <(curl -fsSL "$MW_URL" 2>/dev/null \
#         || wget -qO- "$MW_URL" 2>/dev/null \
#         || python3 -c "import urllib.request as r,sys;sys.stdout.write(r.urlopen('$MW_URL').read().decode())")
#     magicwand init
#
# The image must also have `git` for the pre-release pip install path.
#
# Soft-fail by default: any error prints a message to stderr and exits 0 so
# the WDL task is never broken. Set MAGICWAND_STRICT=1 to propagate errors.
#
# The release tooling rewrites MAGICWAND_VERSION below to the tag at publish
# time. The repo copy ships unpinned for development.

# shellcheck disable=SC2155,SC2317
# SC2317 fires on the deliberate sourced-vs-executed idiom:
#     return 0 2>/dev/null || exit 0
# `return` succeeds when sourced; `exit` fires when executed. Both are reachable
# depending on how the user invoked the script.

# ---- soft-fail boundary ----
__mw_install_strict="${MAGICWAND_STRICT:-0}"
__mw_install_die() {
    echo "magicwand-install: $*" >&2
    if [[ "$__mw_install_strict" == "1" ]]; then
        # When sourced we cannot exit; return non-zero up to the source <().
        return 1 2>/dev/null || exit 1
    fi
    return 0 2>/dev/null || exit 0
}

# ---- version pin ----
# Release tooling replaces this line at publish time.
MAGICWAND_VERSION="${MAGICWAND_VERSION:-}"

# ---- platform detection ----
__mw_uname="$(uname -s 2>/dev/null)"
case "$__mw_uname" in
    Linux|Darwin) ;;
    *) __mw_install_die "unsupported platform: $__mw_uname"; return 0 2>/dev/null || exit 0 ;;
esac

# ---- python resolution ----
# Prefer existing python3 >=3.9. Otherwise use uv to fetch a standalone build.
__mw_python=""
__mw_python_ok() {
    local p="$1"
    [[ -x "$(command -v "$p" 2>/dev/null)" ]] || return 1
    "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
        >/dev/null 2>&1
}

for cand in python3 python3.12 python3.11 python3.10 python3.9; do
    if __mw_python_ok "$cand"; then
        __mw_python="$(command -v "$cand")"
        break
    fi
done

if [[ -z "$__mw_python" ]]; then
    if ! command -v uv >/dev/null 2>&1; then
        # Bootstrap uv. Astral hosts a vetted installer.
        if ! curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
            __mw_install_die "no python3>=3.9 found and uv install failed"
            return 0 2>/dev/null || exit 0
        fi
        # uv installs to ~/.local/bin or ~/.cargo/bin depending on platform.
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
    if ! uv python install 3.11 >/dev/null 2>&1; then
        __mw_install_die "uv python install 3.11 failed"
        return 0 2>/dev/null || exit 0
    fi
    __mw_python="$(uv python find 3.11 2>/dev/null || true)"
    if [[ -z "$__mw_python" ]]; then
        __mw_install_die "uv-managed python not findable after install"
        return 0 2>/dev/null || exit 0
    fi
fi

# ---- pip install ----
# Pre-release: install from the GitHub source archive (no git binary
# required — pip handles .zip URLs natively). The PyPI name `magicwand`
# is squatted by an unrelated 2017 package, so we cannot use it.
__mw_git_ref="${MAGICWAND_GIT_REF:-main}"
__mw_pkg_spec="https://github.com/broadinstitute/magicwand/archive/refs/heads/${__mw_git_ref}.zip"
if [[ -n "$MAGICWAND_VERSION" ]]; then
    __mw_pkg_spec="https://github.com/broadinstitute/magicwand/archive/refs/tags/v${MAGICWAND_VERSION}.zip"
fi

# --break-system-packages is no-op outside PEP 668 environments but the
# flag itself only exists in pip 23+; Ubuntu 22.04 ships pip 22 and would
# reject it. Detect once and add conditionally.
__mw_pip_args=(--user --upgrade --quiet --no-input)
__mw_pip_major="$("$__mw_python" -m pip --version 2>/dev/null | awk '{print $2}' | cut -d. -f1)"
if [[ "$__mw_pip_major" =~ ^[0-9]+$ ]] && (( __mw_pip_major >= 23 )); then
    __mw_pip_args+=(--break-system-packages)
fi

# --quiet hides progress, so emit our own bookend status lines. If the
# install fails, pip still prints the error on stderr (--quiet only
# suppresses progress, not failures).
echo "magicwand: installing $__mw_pkg_spec (silent, ~30-60s)..." >&2
if ! "$__mw_python" -m pip install "${__mw_pip_args[@]}" "$__mw_pkg_spec"; then
    __mw_install_die "pip install of $__mw_pkg_spec failed"
    return 0 2>/dev/null || exit 0
fi
echo "magicwand: installed." >&2

# Make sure the user-site bin is on PATH so magicwand-cli resolves.
__mw_user_base="$("$__mw_python" -m site --user-base 2>/dev/null)"
if [[ -n "$__mw_user_base" ]]; then
    case ":$PATH:" in
        *":$__mw_user_base/bin:"*) ;;
        *) export PATH="$__mw_user_base/bin:$PATH" ;;
    esac
fi

# ---- shell function ----
# Wrap the CLI so `magicwand init` can eval its output into the current shell.
# Other subcommands are passed through unchanged. We capture $$ at call time
# so the shell PID — not the CLI subprocess — is what the sidecar tracks.
magicwand() {
    case "${1:-}" in
        init)
            shift
            local _mw_snippet
            _mw_snippet="$(MAGICWAND_TASK_PID=$$ command magicwand-cli init "$@")" || {
                echo "magicwand: init failed; W&B integration disabled" >&2
                return 0
            }
            eval "$_mw_snippet"
            ;;
        finalize)
            # Set __MW_FINALIZED *before* the CLI runs so the trailing
            # DEBUG/EXIT trap firings are no-ops — the sidecar will be gone
            # when they fire and a write would block on the readerless FIFO.
            command magicwand-cli "$@"
            __MW_FINALIZED=1
            ;;
        "")
            command magicwand-cli --help
            ;;
        *)
            command magicwand-cli "$@"
            ;;
    esac
}

unset __mw_uname __mw_python __mw_pkg_spec __mw_user_base __mw_install_strict
unset -f __mw_install_die __mw_python_ok 2>/dev/null || true
