#!/usr/bin/env bash
# Update an existing AAG - InputLock installation from this checkout.
set -Eeuo pipefail
umask 022

main() {
    local project_dir target_user saved_user
    project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

    if (( EUID != 0 )); then
        printf '%s\n' 'Run with: sudo ./update.sh' >&2
        exit 1
    fi

    target_user=${SUDO_USER:-}
    if [[ -r /etc/input-lock/install-state ]]; then
        saved_user=$(awk -F= '$1=="TARGET_USER" {print $2}' /etc/input-lock/install-state)
        [[ -z "$saved_user" ]] || target_user=$saved_user
    fi

    [[ -n "$target_user" && "$target_user" != root ]] || {
        printf '%s\n' 'Cannot determine target user; run sudo ./install.sh --user LOGIN --update' >&2
        exit 1
    }

    # install.sh checks the whole payload, but root is about to execute
    # install.sh itself: refuse when another account could have changed it.
    # Group write is accepted only for root's group or the owner's private group.
    PYTHONDONTWRITEBYTECODE=1 python3 -I -S - "$project_dir/install.sh" "$(id -u "$target_user")" <<'PY' || {
import grp
import os
import pwd
import stat
import sys

path, target_uid = sys.argv[1], int(sys.argv[2])
allowed = {0, target_uid}
if os.environ.get("SUDO_UID", "").isdigit():
    allowed.add(int(os.environ["SUDO_UID"]))
primary = {}
for entry in pwd.getpwall():
    primary.setdefault(entry.pw_gid, set()).add(entry.pw_name)
problems = []
current = path
while True:
    st = os.lstat(current)
    sticky_root = stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX and st.st_uid == 0
    private = st.st_gid == 0
    if not private:
        try:
            owner = pwd.getpwuid(st.st_uid)
            members = set(grp.getgrgid(st.st_gid).gr_mem) | primary.get(st.st_gid, set())
            private = owner.pw_gid == st.st_gid and members <= {owner.pw_name}
        except KeyError:
            private = False
    if stat.S_ISLNK(st.st_mode):
        problems.append(f"{current}: symbolic link")
    if st.st_uid not in allowed:
        problems.append(f"{current}: owned by UID {st.st_uid}")
    if st.st_mode & stat.S_IWOTH and not sticky_root:
        problems.append(f"{current}: writable by every user")
    if st.st_mode & stat.S_IWGRP and not sticky_root and not private:
        problems.append(f"{current}: writable by shared group GID {st.st_gid}")
    parent = os.path.dirname(current)
    if parent == current:
        break
    current = parent
for problem in problems:
    print("[input-lock] unsafe source: " + problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
        printf '%s\n' '[input-lock] ERROR: install.sh is not safe to run as root from this location' >&2
        exit 1
    }

    exec "$project_dir/install.sh" --user "$target_user" --update
}

main "$@"
