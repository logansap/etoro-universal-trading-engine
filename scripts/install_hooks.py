"""Install the local pre-push hook: every push to GitHub runs scripts/publication_check.py over exactly the commits
being published, and refuses to push any ref under refs/heads/private/ (local-only history).
    python scripts/install_hooks.py
"""
import os
import pathlib
import stat
import subprocess

HOOK = r"""#!/bin/sh
# installed by scripts/install_hooks.py: publication-safety gate for the PUBLIC repository
z=0000000000000000000000000000000000000000
while read local_ref local_sha remote_ref remote_sha; do
  case "$local_ref" in refs/heads/private/*) echo "pre-push: refusing to publish $local_ref (local-only history)"; exit 1;; esac
  [ "$local_sha" = "$z" ] && continue                      # deleting a remote ref: nothing to scan
  if [ "$remote_sha" = "$z" ]; then range="$local_sha --not --remotes"; else range="$remote_sha..$local_sha"; fi
  python scripts/publication_check.py --range "$range" || { echo "pre-push: publication check FAILED for $local_ref"; exit 1; }
done
exit 0
"""


def main():
    hooks = pathlib.Path(subprocess.run(["git", "rev-parse", "--git-path", "hooks"], capture_output=True, text=True,
                                        check=True).stdout.strip())
    hooks.mkdir(parents=True, exist_ok=True)
    p = hooks / "pre-push"
    p.write_text(HOOK, encoding="utf-8", newline="\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {p}")


if __name__ == "__main__":
    main()
