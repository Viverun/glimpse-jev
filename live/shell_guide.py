"""Bash knowledge for the planner's shell steps: a compact guide, exit-code hints, and documentation lookup.

The guide joins the planner prompt only when a task involves the shell (its words, or a shell step already ran),
so other tasks do not pay for it. `help` fetches a command's own --help or man page, so options come from the
installed version instead of memory.
"""

import re

GUIDE = """BASH GUIDE (shell steps run `bash -lc` in your home folder; each step is a fresh shell, 60 s max):
- Chain with && so a failed step stops the rest; `set -o pipefail;` first when a pipe's failure matters.
- Quote every path and variable: "$f", "$HOME/My Files". Use single quotes for literal text, double for $vars.
- Many lines or code for another program: a heredoc.  python3 - <<'PY' ... PY   (quoted 'PY': no $ expansion)
- Find files: find ~ -iname '*report*' -not -path '*/.*' 2>/dev/null | head      (by age: -mmin -10, size: -size +1M)
- Search text: grep -rIn --exclude-dir=.git 'word' DIR | head    (-i any case, -l names only, -w whole word, -E regex)
  grep exits 1 when nothing matched: that is an answer, not an error.
- Edit text in place: sed -i 's/old/new/g' FILE (GNU sed; escape / or use s|a|b|). Columns: awk -F, '{print $2}' f.csv
- Count / sort / unique: wc -l; sort -t, -k2,2n; sort | uniq -c | sort -rn; head -n 20; tail -n 20; cut -d, -f1
- JSON: jq '.items[] | .name' f.json (if jq is missing: python3 -c 'import json,sys; ...').
- Office files: python3 with openpyxl (xlsx), csv, python-docx (docx), odfpy; libreoffice --headless --convert-to
  pdf|xlsx|docx FILE --outdir DIR converts without opening a window (close the file in the app first).
- Files: mkdir -p; cp -n (no overwrite); mv -n; gio trash FILE (recoverable) instead of rm; ls -la; stat; du -sh;
  file FILE (real type); realpath. Archives: tar -xzf a.tgz -C DIR; tar -czf a.tgz DIR; unzip -o a.zip -d DIR.
- Processes: pgrep -af NAME; kill PID (by PID, never pkill a pattern that could match this shell). Something that
  must keep running while later steps work (a server, a long job): a "spawn" step, then "proc" / "stop_proc".
- Desktop: xdg-open FILE|URL (default app); gio open; gsettings get|set SCHEMA KEY; xdg-mime query default TYPE.
- System: sudo -n true (sudo without a password?), apt list --installed 2>/dev/null | grep NAME, systemctl
  --user status UNIT, df -h, free -h, uname -a, cat /etc/os-release, ip -brief addr, ss -ltnp.
- Network: curl -fsSL URL (-o FILE to save; -I headers only); wget -q URL.
- Users / permissions: id; chmod u+x FILE; chown needs sudo; getent passwd NAME.
- Unsure of a command's options: use a "help" step first (its real --help / man page), do not guess flags.
- Verify writes: after changing a file, read it back (cat, head, grep -c) before reporting done."""

TRIGGERS = re.compile(r"\b(terminal|shell|bash|command|commands|script|cron|grep|find|chmod|sudo|install|package|"
                      r"process|service|systemctl|ssh|user|permission|folder|directory|file|files|rename|copy|move|"
                      r"compress|zip|tar|extract|disk|log|logs|env|environment|path)\b", re.I)


def relevant(task: str) -> bool:
    return bool(TRIGGERS.search(task or ""))


EXIT_HINTS = {
    1: "general failure (for grep/diff/test: 'no match' / 'differs' / 'false', which may be the answer)",
    2: "misuse: a wrong option or a syntax error in the command (check with a help step)",
    124: "the command timed out",
    126: "found but not executable (chmod u+x, or run it with bash/python3)",
    127: "command not found: check the name (which NAME, apt list --installed | grep NAME) or use another tool",
    128 + 9: "killed (out of memory or by a timeout)",
    130: "interrupted",
}


def explain(rc: int, out: str) -> str:
    """A short reason to add to a failed shell step, so the next plan fixes the cause instead of retrying."""
    hint = EXIT_HINTS.get(rc, "")
    low = (out or "").lower()
    if "no such file or directory" in low:
        hint = "a path does not exist: list the folder first (ls -la DIR) or find the file"
    elif "permission denied" in low:
        hint = "permission denied: the file is not yours or not writable (ls -l to see; sudo only if allowed)"
    elif "command not found" in low:
        hint = EXIT_HINTS[127]
    elif "syntax error" in low:
        hint = "bash syntax error: check quotes, brackets and heredoc markers"
    elif "modulenotfounderror" in low:
        hint = "a Python module is missing: use another module from the standard library, or pip install --user it"
    return hint


def help_command(name: str) -> str:
    """The bash for a "help" step: the command's own --help, else its man page, trimmed."""
    name = re.sub(r"[^\w.+-]", "", name or "")[:40]
    if not name:
        return ""
    return (f"{{ type {name} 2>&1 | head -1; {{ {name} --help 2>&1 || man -P cat {name} 2>/dev/null; }} "
            f"| sed 's/.\\x08//g' | grep -v '^[[:space:]]*$' | head -n 90; }}")
