# Stellite NX - release packer
# Copyright (C) 2026 Starborne. Part of a work based on Stellite by Jinx; free software under
# the terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.
#
# Run by make_release.bat after Stellite.exe is built from this folder. Makes:
#   release/Stellite-NX-<version>-source.zip   the complete source (what the AGPL calls Corresponding Source)
#   release/Stellite-NX-<version>-windows.zip  Stellite.exe + LICENSE + notices + third-party licenses + the source zip
#
#   python tools/make_release.py [--source-only]

import os
import re
import sys
import zipfile
from importlib import metadata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# everything that is part of the program or of building it (no personal config, downloads or recordings)
SOURCE_FILES = [
    "app.py", "bot.py", "core.py", "engine.py", "charts.py", "chartassist.py", "songdetect.py", "overlay.py", "winio.py",
    "requirements.txt", "setup.bat", "start.bat", "start_app.bat", "build_exe.bat", "make_release.bat",
    "README.md", "LICENSE", "NOTICE.md", "CHANGES.md", ".gitignore", "tools/make_release.py",
]
SOURCE_DIRS = ["assets", "ui"]

# what goes into the exe (build_exe.bat) and so needs its license shipped with it
BUNDLED = ["bettercam", "comtypes", "colorama", "numpy", "opencv-python", "pywin32", "winocr", "pywebview", "pyinstaller"]
LICENSE_NAME = re.compile(r"(^|/)(licen[cs]e|copying|notice|authors)[^/]*$", re.I)


def version():
    with open(os.path.join(ROOT, "core.py"), encoding="utf-8") as f:
        text = f.read()
    v = re.search(r'^VERSION = "([^"]+)"', text, re.M).group(1)
    url = re.search(r'^SOURCE_URL = "([^"]*)"', text, re.M).group(1)
    return v, url


def source_list():
    files = []
    for name in SOURCE_FILES:
        if not os.path.isfile(os.path.join(ROOT, name)):
            sys.exit(f"Missing {name}: the source zip has to be complete")
        files.append(name)
    for folder in SOURCE_DIRS:
        for base, dirs, names in os.walk(os.path.join(ROOT, folder)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in sorted(names):
                files.append(os.path.relpath(os.path.join(base, n), ROOT).replace(os.sep, "/"))
    return files


def make_source_zip(path, prefix):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in source_list():
            z.write(os.path.join(ROOT, name), f"{prefix}/{name}")
    return path


def requirement_names(dist):
    out = []
    for req in dist.requires or []:
        if "extra ==" in req:
            continue
        if ";" in req:
            marker = req.split(";", 1)[1]
            if "sys_platform" in marker and "win32" not in marker and "!=" not in marker:
                continue
        name = re.split(r"[\s<>=!~;\[(]", req.strip(), maxsplit=1)[0]
        if name:
            out.append(name)
    return out


def third_party_licenses():
    """License texts of the bundled packages and everything they depend on, from the installed metadata."""
    seen, todo, parts = set(), list(BUNDLED), []
    while todo:
        name = todo.pop(0)
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        if key == "pyinstaller":
            # only its bootloader ends up in the exe; its dependencies stay on the build PC
            pass
        else:
            todo += requirement_names(dist)
        meta = dist.metadata
        head = f"{meta['Name']} {dist.version}"
        lic = meta.get("License-Expression") or meta.get("License") or ""
        if len(lic) > 200:
            lic = lic.splitlines()[0]
        files = [f for f in dist.files or [] if LICENSE_NAME.search(str(f))]
        in_meta = [f for f in files if ".dist-info/" in str(f)]
        texts, seen_text = [], set()
        for f in in_meta or [f for f in files if str(f).count("/") <= 2]:
            try:
                with open(f.locate(), "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read().strip()
            except OSError:
                continue
            if text and text not in seen_text:
                seen_text.add(text)
                texts.append((str(f), text))
        block = [("=" * 78), head, f"License: {lic or 'see below'}", f"Home: {meta.get('Home-page') or ''}", "=" * 78]
        if texts:
            for fname, text in texts:
                block += [f"--- {fname} ---", text, ""]
        else:
            block += ["(no license file in the installed package; see the license named above)", ""]
        parts.append("\n".join(block))
    py = os.path.join(sys.base_prefix, "LICENSE.txt")
    if os.path.isfile(py):
        with open(py, encoding="utf-8", errors="replace") as f:
            parts.insert(0, "\n".join(["=" * 78, f"Python {sys.version.split()[0]}", "=" * 78, f.read().strip(), ""]))
    return ("Licenses of the software included in Stellite.exe (built with PyInstaller).\n"
            "Stellite NX itself is under the GNU AGPL v3, see LICENSE.\n\n" + "\n\n".join(parts))


def main():
    v, url = version()
    out = os.path.join(ROOT, "release")
    os.makedirs(out, exist_ok=True)
    name = f"Stellite-NX-{v}"
    src = make_source_zip(os.path.join(out, f"{name}-source.zip"), f"{name}-source")
    print(f"Source:  {os.path.relpath(src, ROOT)}")
    if "--source-only" in sys.argv:
        return

    exe = os.path.join(ROOT, "dist", "Stellite.exe")
    if not os.path.isfile(exe):
        sys.exit("dist\\Stellite.exe is missing: run build_exe.bat first (make_release.bat does)")
    newest = max(os.path.getmtime(os.path.join(ROOT, f)) for f in source_list() if not f.endswith((".md", ".gitignore")))
    if os.path.getmtime(exe) < newest:
        sys.exit("Stellite.exe is older than the source: build it again so the exe matches the source zip")

    win = os.path.join(out, f"{name}-windows.zip")
    with zipfile.ZipFile(win, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, f"{name}/Stellite.exe")
        for f in ("LICENSE", "NOTICE.md", "CHANGES.md", "README.md"):
            z.write(os.path.join(ROOT, f), f"{name}/{f}")
        z.writestr(f"{name}/THIRD_PARTY_LICENSES.txt", third_party_licenses())
        z.write(src, f"{name}/{os.path.basename(src)}")
    print(f"Windows: {os.path.relpath(win, ROOT)}")
    if not url:
        print("\nNote: SOURCE_URL in core.py is empty. The source zip ships inside the Windows zip, which is enough,")
        print("but once the release has a page (or a GitHub fork), put its link there so the About screen shows it.")


if __name__ == "__main__":
    main()
