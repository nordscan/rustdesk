#!/usr/bin/env python3
"""Generate .github/workflows/custom-build.yml from upstream flutter-build.yml.

Run from the root of a RustDesk checkout, once per RustDesk version:

    python3 custom/make_workflow.py

The generated workflow keeps only the Windows (x64 + arm64) and macOS
(Intel + Apple Silicon) desktop builds, applies custom/apply_branding.py right
after checkout, renames the outputs to the app name and publishes them as a
GitHub release in your fork. Other upstream workflows (nightly, CI on push, ...)
are removed so they do not burn Actions minutes in the fork.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
WF_DIR = ROOT / ".github/workflows"
SOURCE = WF_DIR / "flutter-build.yml"
TARGET = WF_DIR / "custom-build.yml"
CONFIG_FILE = Path(__file__).resolve().parent / "branding.json"

KEEP_JOBS = ["generate-bridge", "build-RustDeskTempTopMostWindow",
             "build-for-windows-flutter", "build-for-macOS"]
# reusable workflows needed by the kept jobs, plus the generator source
KEEP_FILES = {"bridge.yml", "third-party-RustDeskTempTopMostWindow.yml",
              "flutter-build.yml", "custom-build.yml"}

BRANDING_STEP = """\
      - name: Apply custom branding
        shell: bash
        env:
          RD_PRESET_PASSWORD: ${{ secrets.RD_PRESET_PASSWORD }}
        run: |
          python3 -m venv ../brandenv
          PY=../brandenv/bin/python
          if [ -f ../brandenv/Scripts/python.exe ]; then PY=../brandenv/Scripts/python.exe; fi
          "$PY" -m pip install --quiet pillow
          "$PY" custom/apply_branding.py

"""

# Copying the service binary into the bundle after `flutter build` breaks its
# signature; Apple Silicon refuses to run unsigned code ("app is damaged").
# Approach taken from rdgen: deep ad-hoc signature without hardened runtime.
ADHOC_SIGN_STEP = """\
      - name: Ad-hoc sign app bundle (no Apple certificate)
        if: env.MACOS_P12_BASE64 == ''
        run: |
          APP="./flutter/build/macos/Build/Products/Release/{app}.app"
          xattr -cr "$APP" || true
          codesign --force --deep --sign - "$APP"
          codesign --verify --deep --strict --verbose=2 "$APP"

"""

RENAME_EXE_STEP = """\
      - name: Rename executable to app name
        run: Rename-Item -Path ./rustdesk/rustdesk.exe -NewName "{app}.exe"

"""


def fail(msg):
    print(f"[workflow] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def split_jobs(lines):
    """Return (header_lines, {job_name: lines}, job_order)."""
    try:
        start = lines.index("jobs:")
    except ValueError:
        fail("no 'jobs:' in flutter-build.yml")
    header, jobs, order, current = lines[:start], {}, [], None
    for line in lines[start + 1:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            order.append(current)
            jobs[current] = []
        if current:
            jobs[current].append(line)
    return header, jobs, order


def step_bounds(job, name):
    """Line range [start, end) of the step called `name` in a job block."""
    start = next((i for i, l in enumerate(job) if l.strip() == f"- name: {name}"), None)
    if start is None:
        fail(f"step '{name}' not found, upstream workflow changed")
    end = start + 1
    while end < len(job) and not re.match(r"^      - ", job[end]) and not re.match(r"^  \S", job[end]):
        end += 1
    return start, end


def insert_after(job, name, text):
    _, end = step_bounds(job, name)
    return job[:end] + text.rstrip("\n").split("\n") + [""] + job[end:]


def remove_step(job, name):
    start, end = step_bounds(job, name)
    return job[:start] + job[end:]


def replace_all(job, pairs):
    text = "\n".join(job)
    for old, new in pairs:
        if old not in text:
            fail(f"expected '{old}' in workflow, upstream workflow changed")
        text = text.replace(old, new)
    return text.split("\n")


def main():
    if not SOURCE.exists():
        fail("run this from the root of a RustDesk checkout")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    app = cfg.get("app_name", "RustDesk")
    if not re.match(r"^[A-Za-z][A-Za-z0-9]{1,30}$", app):
        fail("invalid app_name in branding.json")
    slug = app.lower()
    company = cfg.get("company", "").strip() or app
    if re.search(r"[\"'`$\\]", company):
        fail("company must not contain quotes, backticks, $ or backslashes")
    msi_args = f'--app-name {app} -m "{company}"'
    if cfg.get("connection_type", "both") in ("incoming", "outgoing"):
        msi_args += f" --conn-type {cfg['connection_type']}"

    lines = SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    header, jobs, order = split_jobs(lines)
    for j in KEEP_JOBS:
        if j not in jobs:
            fail(f"job '{j}' not found, upstream workflow changed")

    version = next((m.group(1) for l in header
                    if (m := re.match(r'^\s+VERSION:\s*"([^"]+)"', l))), None)
    if not version:
        fail("VERSION not found in workflow env")

    # ---- header: name, trigger, permissions
    text = "\n".join(header)
    text = re.sub(r"^name: .*$", f"name: Custom build ({app})", text, count=1, flags=re.M)
    trigger = (
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      upload-artifact:\n"
        "        type: boolean\n"
        "        default: true\n"
        "      upload-tag:\n"
        "        description: Release tag in this repo for the built installers\n"
        "        type: string\n"
        f"        default: \"{slug}-{version}\"\n"
        "\n"
        "permissions:\n"
        "  contents: write\n"
    )
    text, n = re.subn(r"^on:\n(?:[ \t].*\n|\n)*", trigger + "\n", text, count=1, flags=re.M)
    if n != 1:
        fail("could not replace 'on:' block")
    out = [f"# GENERATED by custom/make_workflow.py from flutter-build.yml ({version}). Do not edit.",
           *text.split("\n"), "jobs:"]

    # ---- Windows
    win = jobs["build-for-windows-flutter"]
    win = insert_after(win, "Checkout source code", BRANDING_STEP)
    win = remove_step(win, "Upload unsigned msi template")
    win = remove_step(win, "Build pre-built MSI template")
    if slug != "rustdesk":
        win = insert_after(win, "Build rustdesk", RENAME_EXE_STEP.format(app=app))
    win = replace_all(win, [
        ("-e ../../rustdesk/rustdesk.exe", f"-e ../../rustdesk/{app}.exe"),
        ("python preprocess.py --arp -d ../../rustdesk\n",
         f"python preprocess.py --arp -d ../../rustdesk {msi_args}\n"),
        ("rustdesk-${{ env.VERSION }}", f"{slug}-${{{{ env.VERSION }}}}"),
        ("SignOutput/rustdesk-*", f"SignOutput/{slug}-*"),
    ])
    jobs["build-for-windows-flutter"] = win

    # ---- macOS
    mac = jobs["build-for-macOS"]
    mac = insert_after(mac, "Checkout source code", BRANDING_STEP)
    mac = insert_after(mac, "Build rustdesk", ADHOC_SIGN_STEP.format(app=app))
    mac = replace_all(mac, [
        ("RustDesk.app", f"{app}.app"),
        ("rustdesk-${{ env.VERSION }}-${{ matrix.job.arch }}.dmg", f"{slug}-${{{{ env.VERSION }}}}.dmg"),
        ("rustdesk-${{ env.VERSION }}", f"{slug}-${{{{ env.VERSION }}}}"),
        ("rustdesk*", f"{slug}*"),
    ])
    jobs["build-for-macOS"] = mac

    for j in order:
        if j in KEEP_JOBS:
            out.extend(jobs[j])
    TARGET.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    print(f"[workflow] wrote {TARGET.relative_to(ROOT)}")

    for f in sorted(WF_DIR.glob("*.yml")):
        if f.name not in KEEP_FILES:
            f.unlink()
            print(f"[workflow] removed {f.relative_to(ROOT)}")

    # upstream .gitignore ignores *png, which would silently drop custom/assets
    gitignore = ROOT / ".gitignore"
    rule = "!custom/assets/*.png"
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if rule not in text.split("\n"):
        gitignore.write_text(text.rstrip("\n") + "\n" + rule + "\n", encoding="utf-8", newline="\n")
        print(f"[workflow] added '{rule}' to .gitignore")


if __name__ == "__main__":
    main()
