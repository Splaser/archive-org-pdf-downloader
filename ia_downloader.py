#!/usr/bin/env python3

import os
import sys
import json
import hashlib
import re
import shutil
import subprocess
import time
import requests
from urllib.parse import quote, unquote



BAD_SUFFIX = (
    "_text.pdf",
    "_djvu.pdf",
    "_bw.pdf",
    "_jp2.pdf",
)

STATE_FILE = ".ia_downloader_state.json"
ISSUE_PATTERN = re.compile(r"(?<!\d)(\d{3})(?!\d)")


def get_identifier(url):
    """
    https://archive.org/details/pocketgamer/xxx
    -> pocketgamer
    """
    parts = url.rstrip("/").split("/")

    idx = parts.index("details")
    return parts[idx + 1]


def request_json(url, params=None):
    """Fetch JSON with bounded retries for transient Archive.org failures."""
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Archive.org request failed after 3 attempts: {last_error}")


def find_alternative_identifiers(identifier):
    data = request_json(
        "https://archive.org/advancedsearch.php",
        params={
            "q": f"identifier:{identifier}*",
            "fl[]": ["identifier", "title"],
            "rows": 20,
            "output": "json",
        },
    )
    docs = data.get("response", {}).get("docs", [])
    prefix = identifier.lower()

    return sorted({
        doc["identifier"]
        for doc in docs
        if doc.get("identifier", "").lower().startswith(prefix)
        and doc["identifier"].lower() != prefix
    })


def get_files(identifier):

    api = f"https://archive.org/metadata/{identifier}"
    data = request_json(api)

    if not data.get("files"):
        alternatives = find_alternative_identifiers(identifier)

        if len(alternatives) == 1:
            resolved_identifier = alternatives[0]
            print(
                f"'{identifier}' has no public files; "
                f"using '{resolved_identifier}' instead."
            )
            identifier = resolved_identifier
            data = request_json(
                f"https://archive.org/metadata/{identifier}"
            )
        else:
            state = "dark/unavailable" if data.get("is_dark") else "empty/unavailable"
            suggestion = (
                f" Similar identifiers: {', '.join(alternatives)}"
                if alternatives else ""
            )
            raise RuntimeError(
                f"Archive.org item '{identifier}' is {state} and has no public files."
                f"{suggestion}"
            )

    if not data.get("files"):
        raise RuntimeError(
            f"Archive.org item '{identifier}' returned no downloadable files."
        )

    pdf = []
    epub = []

    for f in data["files"]:

        name = f.get("name", "")

        lower = name.lower()

        if lower.endswith(".pdf"):

            # 排除 IA 派生 OCR PDF
            if lower.endswith(BAD_SUFFIX):
                continue

            pdf.append(file_info(f))

        elif lower.endswith(".epub"):

            epub.append(file_info(f))

    pdf = keep_largest_pdf_per_issue(pdf)

    return pdf, epub, identifier


def file_info(metadata):
    """Keep the fields needed to decide whether a download is complete."""
    size = metadata.get("size")

    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None

    return {
        "name": metadata["name"],
        "size": size,
        "md5": metadata.get("md5"),
    }


def get_issue_id(filename):
    """Return an independent 3-digit issue number, such as 002 or VOL.109."""
    match = ISSUE_PATTERN.search(filename)
    return match.group(1) if match else None


def keep_largest_pdf_per_issue(files):
    """For metadata variants of one issue, keep only the largest PDF."""
    best_by_issue = {}

    for item in files:
        issue_id = get_issue_id(item["name"])
        if issue_id is None:
            continue

        current = best_by_issue.get(issue_id)
        item_size = item.get("size") if item.get("size") is not None else -1
        current_size = (
            current.get("size")
            if current and current.get("size") is not None
            else -1
        )

        if current is None or item_size > current_size:
            best_by_issue[issue_id] = item

    selected = [
        item for item in files
        if get_issue_id(item["name"]) is None
        or best_by_issue[get_issue_id(item["name"])] is item
    ]
    removed_count = len(files) - len(selected)

    if removed_count:
        print(f"Smaller PDF variants skipped: {removed_count}")

    return selected


def remove_duplicate_pdfs(outdir):
    """Delete smaller already-downloaded PDFs sharing the same issue number."""
    groups = {}

    for entry in os.scandir(outdir):
        if not entry.is_file() or not entry.name.lower().endswith(".pdf"):
            continue

        issue_id = get_issue_id(entry.name)
        if issue_id is not None:
            groups.setdefault(issue_id, []).append(entry.path)

    removed = []
    for issue_id, paths in groups.items():
        if len(paths) < 2:
            continue

        keep = max(paths, key=lambda path: (os.path.getsize(path), path))
        print(f"Issue {issue_id}, keeping largest: {os.path.basename(keep)}")

        for path in paths:
            if path == keep:
                continue

            os.remove(path)
            control_file = path + ".aria2"
            if os.path.exists(control_file):
                os.remove(control_file)
            removed.append(os.path.basename(path))
            print(f"Removed smaller duplicate: {os.path.basename(path)}")

    return removed


def build_download_url(identifier, filename):
    return (
        "https://archive.org/download/"
        f"{identifier}/"
        f"{quote(filename)}"
    )


def aria2_download(url, outdir="."):

    cmd = [
        "aria2c",
        "-x", "16",
        "-s", "16",
        "-d", outdir,
        url
    ]

    subprocess.run(cmd)


def requests_download(url, outdir="."):

    name = unquote(url.split("/")[-1])

    path = os.path.join(outdir, name)
    partial_path = path + ".part"

    print("fallback:", path)

    with requests.get(
        url,
        stream=True
    ) as r:

        r.raise_for_status()

        with open(partial_path, "wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk:
                    f.write(chunk)

    os.replace(partial_path, path)

    print("done")


def download(url, outdir):

    if shutil.which("aria2c"):

        print("use aria2")

        aria2_download(
            url,
            outdir
        )

    else:

        print("use requests")

        requests_download(
            url,
            outdir
        )


def run_aria2(input_file):

    if not shutil.which("aria2c"):
        return False

    cmd = [
        "aria2c",
        "-i",
        input_file,
        "-x",
        "16",
        "-s",
        "16",
        "--continue=true",
        "--check-integrity=true",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--max-tries=5",
        "--retry-wait=10",
    ]

    result = subprocess.run(cmd)

    return result.returncode == 0


def run_requests_fallback(files, identifier, outdir):

    for item in files:

        name = item["name"]

        url = build_download_url(
            identifier,
            name
        )

        requests_download(
            url,
            outdir
        )


def download_files(
        identifier,
        files,
        input_name,
        outdir):

    pending = find_pending_downloads(files, outdir)
    skipped = len(files) - len(pending)

    print(f"Already complete, skipped: {skipped}")
    print(f"Need download/retry: {len(pending)}")

    if not pending:
        print(
            f"All files for {input_name} are complete"
        )
        return True


    generate_aria2_input(
        identifier,
        pending,
        input_name
    )


    if shutil.which("aria2c"):
        success = run_aria2(input_name)
        if not success:
            print("Some downloads failed; run the same command again to retry only incomplete files.")
        return success


    print(
        "aria2 unavailable, fallback requests"
    )

    run_requests_fallback(
        pending,
        identifier,
        outdir
    )

    return True


def calculate_md5(path):
    digest = hashlib.md5()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def is_download_complete(item, outdir, checksum_cache=None):
    """A same-named partial/preallocated file must not be treated as done."""
    path = os.path.join(outdir, item["name"])

    if not os.path.isfile(path) or os.path.exists(path + ".aria2"):
        return False

    stat = os.stat(path)
    expected_size = item.get("size")
    if expected_size is None:
        size_matches = stat.st_size > 0
    else:
        size_matches = stat.st_size == expected_size

    if not size_matches:
        return False

    expected_md5 = item.get("md5")
    if not expected_md5:
        return True

    cache = checksum_cache if checksum_cache is not None else {}
    cached = cache.get(item["name"], {})

    if (
        cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and cached.get("md5")
    ):
        actual_md5 = cached["md5"]
    else:
        print(f"Verifying: {item['name']}")
        actual_md5 = calculate_md5(path)
        cache[item["name"]] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "md5": actual_md5,
        }

    return actual_md5.lower() == expected_md5.lower()


def find_pending_downloads(files, outdir):
    state_path = os.path.join(outdir, STATE_FILE)

    try:
        with open(state_path, "r", encoding="utf8") as f:
            checksum_cache = json.load(f)
        if not isinstance(checksum_cache, dict):
            checksum_cache = {}
    except (OSError, ValueError):
        checksum_cache = {}

    pending = [
        item for item in files
        if not is_download_complete(item, outdir, checksum_cache)
    ]

    temp_state_path = state_path + ".tmp"
    try:
        with open(temp_state_path, "w", encoding="utf8") as f:
            json.dump(checksum_cache, f, ensure_ascii=False, indent=2)
        os.replace(temp_state_path, state_path)
    except OSError as error:
        print(f"Warning: could not save checksum cache: {error}")

    return pending


def generate_aria2_input(
        identifier,
        files,
        filename):

    with open(filename, "w", encoding="utf8") as f:

        for item in files:

            name = item["name"]

            url = build_download_url(
                identifier,
                name
            )

            f.write(url + "\n")
            f.write("  dir=downloads\n")
            f.write(f"  out={name}\n")

            if item.get("md5"):
                # aria2 verifies retried files before reporting success.
                f.write(f"  checksum=md5={item['md5']}\n")

            f.write("\n")


if __name__ == "__main__":

    archive_url = sys.argv[1]

    outdir = "./downloads"

    os.makedirs(
        outdir,
        exist_ok=True
    )

    remove_duplicate_pdfs(outdir)


    identifier = get_identifier(
        archive_url
    )


    print(
        "identifier:",
        identifier
    )


    pdf_files, epub_files, identifier = get_files(
        identifier
    )


    print(
        f"PDF: {len(pdf_files)}"
    )

    print(
        f"EPUB: {len(epub_files)}"
    )


    success = download_files(
        identifier,
        pdf_files,
        "aria2_pdf.txt",
        outdir
    )

    if not success:
        sys.exit(1)
