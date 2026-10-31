#!/usr/bin/env python3
"""Inspect anonymous delivery files recursively, including PDF and PPTX metadata."""
import re
import shutil
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {".py", ".sh", ".c", ".h", ".md", ".json", ".jsonl", ".csv", ".txt", ".log", ".srt", ".yaml", ".yml", ".toml", ""}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PRIVATE_PATH = re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+/|(?<![A-Za-z0-9_}/])/root/[A-Za-z0-9_.-]+/")
IDENTITY_LABEL = re.compile(r"(?:学校名称|所在学校|指导教师姓名|导师姓名|作者姓名|学生姓名|学号|电子邮箱|联系电话)\s*[:：]")
GENERATED = {".pyc", ".pyo", ".fdb_latexmk", ".fls", ".tex", ".cls", ".sty", ".aux"}


def submitted(path):
    relative = path.relative_to(ROOT)
    if any(p in {".git", "__pycache__", "build", ".pytest_cache"} or p.startswith(".venv") for p in relative.parts):
        return False
    if relative.parts[0] == "results" and relative.name != "README.md":
        return False
    return True


def main():
    for command in ("pdftotext", "pdfinfo"):
        if not shutil.which(command):
            raise SystemExit(f"Required audit dependency is missing: {command}")
    problems = []
    count = {"text": 0, "pdf": 0, "pptx": 0}

    def check_text(text, relative):
        if str(relative).startswith("docs/licenses/"):
            return
        # Public mailing-list Message-ID in the LockCoop bibliography, not a team identity.
        text = text.replace("cover.1770760558.git.tim.c.chen@linux.intel.com", "<upstream-message-id>")
        for rule, pattern in (("email address", EMAIL), ("private absolute path", PRIVATE_PATH), ("identity field", IDENTITY_LABEL)):
            if pattern.search(text):
                problems.append(f"{relative}: {rule}")

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or not submitted(path):
            continue
        relative = path.relative_to(ROOT)
        if path.suffix in GENERATED:
            problems.append(f"{relative}: generated/writing source in delivery")
        if path.suffix == ".pdf":
            count["pdf"] += 1
            info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
            author = re.search(r"^Author:[ \t]*(.*)$", info, re.MULTILINE)
            if author and author.group(1).strip():
                problems.append(f"{relative}: non-empty PDF Author metadata")
            check_text(info, relative)
            extracted = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout
            check_text(extracted.replace("\x00", ""), relative)
        elif path.suffix == ".pptx":
            count["pptx"] += 1
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml"):
                        xml = ET.fromstring(archive.read(name))
                        check_text(" ".join(xml.itertext()), relative)
                        if name == "docProps/core.xml":
                            for node in xml:
                                if node.tag.split("}")[-1] in {"creator", "lastModifiedBy"} and (node.text or "").strip():
                                    problems.append(f"{relative}: non-empty author/editor metadata")
        elif path.suffix in TEXT_EXTENSIONS:
            count["text"] += 1
            check_text(path.read_text(encoding="utf-8", errors="replace"), relative)
    for problem in problems:
        print("PUBLIC AUDIT FAILED:", problem)
    if problems:
        raise SystemExit(1)
    print(f"Public audit passed: {count['text']} text files, {count['pdf']} PDFs, {count['pptx']} PPTX files.")
    print("Scope: current delivery files; required upstream license notices retained.")


if __name__ == "__main__":
    main()
