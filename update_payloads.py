"""Incremental, verified GitHub/Gitea release mirror using Python's standard library."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import zipfile

TAG = "payloads-mirror"
MAX_BYTES = 128 * 1024 * 1024
START = "<!-- PAYLOADS_START -->"
END = "<!-- PAYLOADS_END -->"
RETRY_CODES = {429, 500, 502, 503, 504}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".mirror-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def write_json(path, data):
    return atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else deepcopy(default)


def repository_name(explicit=None):
    name = explicit or os.environ.get("GITHUB_REPOSITORY")
    if not name:
        result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                                text=True, encoding="utf-8", check=False, timeout=10)
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip())
        name = match.group(1) if match else None
    if not name or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("Provide --repository OWNER/REPO or set GITHUB_REPOSITORY")
    return name


def mirror_url(repository, filename):
    return f"https://github.com/{repository}/releases/download/{TAG}/{quote(filename, safe='')}"


def safe_component(value):
    component = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    if not component:
        raise ValueError(f"Invalid filename component: {value!r}")
    return component


def asset_matches(asset, checksum, size):
    return (asset.get("state", "uploaded") == "uploaded" and asset.get("size") == size
            and asset.get("digest") in (None, "", "sha256:" + checksum))


class HttpClient:
    def __init__(self, options):
        self.options = options
        self.cache_path = options.root / ".cache/releases.json"
        self.cache = load_json(self.cache_path, {})
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def validate_url(self, url):
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise ValueError("Credentials must not appear in a URL")
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (self.options.allow_local_http and loopback
                                                and parsed.scheme == "http"):
            raise ValueError(f"HTTPS is required: {url}")

    def headers(self, url, api=False):
        headers = {"User-Agent": "ps5-payloads-mirror-hourly/1.0"}
        # Never send the GitHub token to upstream downloads or a Gitea host.
        trusted = urlsplit(url).netloc in {urlsplit(self.options.api_base).netloc,
                                          urlsplit(self.options.upload_base).netloc}
        if api and trusted:
            headers.update({"Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"})
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        return headers

    def request(self, url, *, method="GET", data=None, headers=None, api=False):
        self.validate_url(url)
        merged = self.headers(url, api)
        merged.update(headers or {})
        for attempt in range(self.options.retries):
            try:
                return urlopen(Request(url, data=data, headers=merged, method=method),
                               timeout=self.options.timeout)
            except HTTPError as error:
                retry = error.code in RETRY_CODES or (
                    error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0")
                if not retry or attempt + 1 == self.options.retries:
                    raise
                retry_after = error.headers.get("Retry-After", "0")
                delay = float(retry_after) if retry_after.isdigit() else 0
                error.close()
            except (URLError, TimeoutError, socket.timeout, OSError):
                if attempt + 1 == self.options.retries:
                    raise
                delay = 0
            time.sleep(min(30, max(delay, self.options.retry_delay * 2**attempt)))

    def json(self, url, *, cached=False, method="GET", body=None, api=True):
        headers = {}
        previous = self.cache.get(url)
        if cached and previous and previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with self.request(url, method=method, data=data, headers=headers, api=api) as response:
                content = json.loads(response.read(16 * 1024 * 1024).decode("utf-8"))
                if cached:
                    self.cache[url] = {"etag": response.headers.get("ETag"), "body": content}
                return content
        except HTTPError as error:
            if error.code == 304 and previous:
                error.close()
                return deepcopy(previous["body"])
            raise

    def download(self, asset, path):
        size = asset.get("size")
        if isinstance(size, int) and (size <= 0 or size > MAX_BYTES):
            raise ValueError("Upstream asset is empty or exceeds the 128 MiB limit")
        for attempt in range(self.options.retries):
            try:
                digest = hashlib.sha256()
                total = 0
                with self.request(asset["browser_download_url"]) as response, path.open("wb") as output:
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError("Download exceeds the 128 MiB limit")
                        output.write(chunk)
                        digest.update(chunk)
                if total == 0 or (isinstance(size, int) and total != size):
                    raise ValueError(f"Asset size mismatch: expected={size}, received={total}")
                expected = asset.get("digest")
                if expected and expected.startswith("sha256:") and expected != "sha256:" + digest.hexdigest():
                    raise ValueError("Upstream SHA-256 digest mismatch")
                return
            except (URLError, TimeoutError, OSError, http.client.IncompleteRead):
                if attempt + 1 == self.options.retries:
                    raise
                time.sleep(min(30, self.options.retry_delay * 2**attempt))

    def save_cache(self):
        write_json(self.cache_path, self.cache)


def latest_release(client, item):
    source = urlsplit(item["source"])
    parts = source.path.strip("/").split("/")
    if len(parts) < 2 or source.username or source.password:
        raise ValueError("source must identify a GitHub or Gitea repository")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid upstream owner or repository")
    github = source.hostname == "github.com"
    base = client.options.api_base if github else f"{source.scheme}://{source.netloc}/api/v1"
    releases = []
    for page in range(1, 11):
        query = {"per_page" if github else "limit": 100, "page": page}
        url = f"{base}/repos/{owner}/{repo}/releases?{urlencode(query)}"
        batch = client.json(url, cached=True, api=github)
        if not isinstance(batch, list):
            raise ValueError("Upstream releases API did not return a list")
        releases.extend(batch)
        if len(batch) < 100:
            break
    eligible = [release for release in releases if not release.get("draft")
                and (item.get("include_prereleases", False) or not release.get("prerelease"))]
    if not eligible:
        raise ValueError("No published release satisfies this payload's release policy")
    release = max(eligible, key=lambda r: (r.get("published_at") or r.get("created_at") or "", r.get("id", 0)))
    if not release.get("tag_name") or not release.get("assets"):
        raise ValueError("Latest published release has no downloadable assets yet")
    return release, repo


def select_asset(item, release, repo):
    pattern = re.compile(item["asset_pattern"], re.IGNORECASE) if item.get("asset_pattern") else None
    preferred = ".bin" if item["name"].lower() == "etahen" else ".elf"
    candidates = []
    for asset in release["assets"]:
        name = asset.get("name", "")
        suffix = PurePosixPath(name.lower()).suffix
        if suffix not in {".elf", ".bin", ".zip"} or asset.get("state", "uploaded") != "uploaded":
            continue
        if pattern:
            if not pattern.search(name):
                continue
        else:
            lower = name.lower()
            if "ps4" in lower and "ps5" not in lower:
                continue
            if any(word in lower for word in ("helper", "stub")):
                continue
            if "install" in lower and "install" not in (item["name"] + repo).lower():
                continue
        candidates.append(asset)
    if not candidates:
        raise ValueError("No suitable PS5 asset; check asset_pattern")
    previous = PurePosixPath(urlsplit(item.get("source_direct", "")).path).name
    old = str(item.get("version", ""))
    new = str(release["tag_name"])
    expected = {previous}
    if previous and old:
        expected.update({previous.replace(old, new), previous.replace(old.lstrip("v"), new.lstrip("v"))})
    continuity = [asset for asset in candidates if asset["name"] in expected]
    if len(continuity) == 1:
        return continuity[0]
    if len(candidates) == 1:
        return candidates[0]

    def score(asset):
        lower = asset["name"].lower()
        extension = PurePosixPath(lower).suffix
        value = 20 if extension == ".zip" and item.get("extract_file") else 0
        value += 5 if extension == preferred else 0
        value += 10 if "ps5" in lower else 0
        words = set(re.findall(r"[a-z0-9]+", (item["name"] + " " + repo).lower())) - {"ps5", "ps4", "payload", "release"}
        value += sum(3 for word in words if len(word) > 2 and word in lower)
        return value

    ordered = sorted(candidates, key=score, reverse=True)
    if score(ordered[0]) == score(ordered[1]):
        raise ValueError("Ambiguous upstream assets; set an explicit asset_pattern")
    return ordered[0]


def signature(release, asset):
    return {"release_id": release.get("id"), "id": asset.get("id"), "name": asset["name"],
            "size": asset.get("size"), "updated_at": asset.get("updated_at"),
            "digest": asset.get("digest"), "url": asset["browser_download_url"]}


def extract_payload(archive_path, destination, configured=None):
    with zipfile.ZipFile(archive_path) as archive:
        candidates = [member for member in archive.infolist() if not member.is_dir()
                      and PurePosixPath(member.filename.lower()).suffix in {".elf", ".bin"}]
        if configured:
            matches = [member for member in candidates if member.filename == configured]
            if not matches:
                matches = [member for member in candidates if PurePosixPath(member.filename).name == configured]
        else:
            matches = candidates
        if len(matches) != 1:
            raise ValueError("ZIP needs a single payload or an exact extract_file")
        member = matches[0]
        parts = PurePosixPath(member.filename).parts
        if member.filename.startswith(("/", "\\")) or ".." in parts or "\\" in member.filename or ":" in member.filename:
            raise ValueError("Unsafe ZIP payload path")
        if member.file_size <= 0 or member.file_size > MAX_BYTES:
            raise ValueError("ZIP payload is empty or exceeds the 128 MiB limit")
        total = 0
        with archive.open(member) as source, destination.open("wb") as output:
            while chunk := source.read(64 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("ZIP payload exceeds the 128 MiB limit")
                output.write(chunk)
        if total != member.file_size:
            raise ValueError("ZIP payload size mismatch")
        return member.filename, PurePosixPath(member.filename.lower()).suffix


class Publisher:
    def __init__(self, client, repository, publish):
        self.client = client
        self.repository = repository
        self.base = f"{client.options.api_base}/repos/{repository}"
        self.release = None
        try:
            self.release = client.json(f"{self.base}/releases/tags/{TAG}")
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
        if self.release is None and publish:
            self.release = client.json(f"{self.base}/releases", method="POST", body={
                "tag_name": TAG, "target_commitish": os.environ.get("MIRROR_BRANCH", "main"),
                "name": "PS5 Payloads Mirror", "body": "Verified payload mirror, checked hourly.\n\n"
                "Current files and SHA-256 checksums are listed in payloads.json. "
                "Previous versions are retained for rollback.", "draft": False, "prerelease": False})
        self.assets = self.list_assets()

    def list_assets(self):
        if self.release is None:
            return {}
        assets = {}
        for page in range(1, 101):
            batch = self.client.json(f"{self.base}/releases/{self.release['id']}/assets?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("Mirror assets API did not return a list")
            assets.update({asset["name"]: asset for asset in batch})
            if len(batch) < 100:
                return assets
        raise ValueError("Mirror asset pagination exceeded 100 pages")

    def upload(self, path, checksum):
        existing = self.assets.get(path.name)
        if existing:
            if asset_matches(existing, checksum, path.stat().st_size):
                return
            raise ValueError("Refusing to overwrite a release asset with different bytes")
        url = (f"{self.client.options.upload_base}/repos/{self.repository}/releases/"
               f"{self.release['id']}/assets?{urlencode({'name': path.name})}")
        try:
            with self.client.request(url, method="POST", data=path.read_bytes(),
                                     headers={"Content-Type": "application/octet-stream"}, api=True) as response:
                asset = json.loads(response.read().decode("utf-8"))
            if not asset_matches(asset, checksum, path.stat().st_size):
                raise ValueError("Uploaded release asset failed size/digest verification")
            self.assets[path.name] = asset
        except (HTTPError, URLError, OSError, http.client.IncompleteRead):
            self.assets = self.list_assets()
            recovered = self.assets.get(path.name)
            if recovered and asset_matches(recovered, checksum, path.stat().st_size):
                return
            raise


def update_readme(root=None, repository=None, payloads=None):
    root = Path(root or ".")
    payloads = payloads if payloads is not None else load_json(root / "payloads.json", [])
    repository = repository or repository_name()

    def cell(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    rows = ["| Payload | Version | Description | Last Updated | Source | Download |",
            "| --- | --- | --- | --- | --- | --- |"]
    for item in payloads:
        name = cell(item["name"])
        version = cell(item.get("version", "pending"))
        description = cell(item.get("description", ""))
        date = cell(item.get("last_update", "pending"))
        source = item.get("source", "#")
        download = f"[Download]({item['url']})" if item.get("url") else "Pending first sync"
        rows.append(f"| **{name}** | `{version}` | {description} | `{date}` | [Source]({source}) | {download} |")
    table = START + "\n" + "\n".join(rows) + "\n" + END
    path = root / "README.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "# PS5 Payloads Mirror\n\n## Available Payloads\n\n"
    if START in text and END in text:
        first = text.index(START)
        last = text.index(END, first) + len(END)
        text = text[:first] + table + text[last:]
    else:
        text += "\n" + table + "\n"
    atomic_write(path, text)


def refresh_health(root, repository, updated):
    path = root / "mirror-health.json"
    previous = load_json(path, {})
    try:
        then = datetime.fromisoformat(previous.get("last_successful_check", ""))
    except (ValueError, TypeError):
        then = datetime.min.replace(tzinfo=timezone.utc)
    if updated or (datetime.now(timezone.utc) - then).days >= 28:
        write_json(path, {"repository": repository, "last_successful_check": utc_now(),
                          "note": "Successful full sync; refreshed on changes or every 28 days."})


def run(options):
    repository = repository_name(options.repository)
    if options.publish and not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")) and not options.allow_local_http:
        raise ValueError("--publish requires GH_TOKEN or GITHUB_TOKEN with contents:write")
    items = load_json(options.root / "payloads.json")
    if not isinstance(items, list) or not items:
        raise ValueError("payloads.json must be a non-empty list")
    names = [item.get("name") for item in items]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("Every payload needs a unique, non-empty name")
    if options.target and options.target not in names:
        raise ValueError(f"Unknown payload name: {options.target}")
    client = HttpClient(options)
    publisher = Publisher(client, repository, options.publish)
    report = {"started_at": utc_now(), "repository": repository,
              "mode": "publish" if options.publish else ("download-only" if options.download_only else "dry-run"),
              "checked": 0, "updated": 0, "unchanged": 0, "failed": 0, "entries": []}
    proposed = deepcopy(items)
    for index, item in enumerate(items):
        if options.target and item["name"] != options.target:
            continue
        result = {"name": item["name"], "previous_version": item.get("version")}
        report["checked"] += 1
        print(f"Checking {item['name']}...", flush=True)
        try:
            release, repo = latest_release(client, item)
            asset = select_asset(item, release, repo)
            state = signature(release, asset)
            version = release["tag_name"]
            result["version"] = version
            remote = publisher.assets.get(item.get("filename"))
            unchanged = (item.get("version") == version and item.get("source_asset") == state
                         and item.get("url") == mirror_url(repository, item.get("filename", ""))
                         and remote and item.get("checksum") and item.get("payload_size")
                         and asset_matches(remote, item["checksum"], item["payload_size"]))
            if unchanged:
                result.update({"status": "unchanged", "filename": item["filename"]})
                report["unchanged"] += 1
            elif not options.publish and not options.download_only:
                result.update({"status": "update-available", "source_direct": asset["browser_download_url"]})
                report["updated"] += 1
            else:
                directory = options.root / "payloads"
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".download-", dir=directory) as temporary:
                    original = Path(temporary) / "asset"
                    client.download(asset, original)
                    extension = PurePosixPath(asset["name"].lower()).suffix
                    extracted = Path(temporary) / "payload"
                    member = None
                    if extension == ".zip":
                        member, extension = extract_payload(original, extracted, item.get("extract_file"))
                    else:
                        extracted = original
                    with extracted.open("rb") as binary:
                        prefix = binary.read(16)
                    if not prefix or (extension == ".elf" and not prefix.startswith(b"\x7fELF")):
                        raise ValueError("Downloaded payload is not a valid non-empty ELF/binary")
                    checksum = hashlib.sha256(extracted.read_bytes()).hexdigest()
                    size = extracted.stat().st_size
                    filename = f"{safe_component(item['name'])}_{safe_component(version)}{extension}"
                    existing = publisher.assets.get(filename)
                    if existing and not asset_matches(existing, checksum, size):
                        filename = f"{safe_component(item['name'])}_{safe_component(version)}_{checksum[:12]}{extension}"
                    target = directory / filename
                    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                        filename = f"{safe_component(item['name'])}_{safe_component(version)}_{checksum[:12]}{extension}"
                        target = directory / filename
                    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                        raise ValueError("Local immutable filename collision")
                    os.replace(extracted, target)
                if options.publish:
                    publisher.upload(target, checksum)
                new = deepcopy(item)
                new.update({"filename": filename, "url": mirror_url(repository, filename),
                            "version": version, "last_update": (release.get("published_at") or release["created_at"])[:10],
                            "source_direct": asset["browser_download_url"], "checksum": checksum,
                            "payload_size": size, "source_asset": state})
                if member:
                    new["extract_file"] = member
                else:
                    new.pop("extract_file", None)
                proposed[index] = new
                result.update({"status": "updated" if options.publish else "downloaded", "filename": filename,
                               "checksum": checksum, "payload_size": size})
                report["updated"] += 1
        except (HTTPError, URLError, ValueError, KeyError, TypeError, OSError,
                http.client.HTTPException, zipfile.BadZipFile, RuntimeError) as error:
            result.update({"status": "failed", "error": str(error)})
            report["failed"] += 1
            print(f"  ERROR: {error}", flush=True)
        report["entries"].append(result)
        print(f"  {result['status']}", flush=True)
    if options.publish:
        proposed.sort(key=lambda item: item.get("last_update", ""), reverse=True)
        write_json(options.root / "payloads.json", proposed)
        update_readme(options.root, repository, proposed)
        sums = "".join(f"{item['checksum']}  {item['filename']}\n" for item in proposed
                       if item.get("checksum") and item.get("filename"))
        atomic_write(options.root / "SHA256SUMS", sums)
        if report["failed"] == 0 and not options.target:
            refresh_health(options.root, repository, report["updated"])
    elif options.download_only:
        write_json(options.root / "reports/staged-payloads.json", proposed)
    client.save_cache()
    report["finished_at"] = utc_now()
    write_json(options.root / "reports/sync-report.json", report)
    print(f"Checked={report['checked']} updated={report['updated']} unchanged={report['unchanged']} failed={report['failed']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["## PS5 payload mirror", f"Checked: {report['checked']} · Updated: {report['updated']} · "
                 f"Unchanged: {report['unchanged']} · Failed: {report['failed']}", "",
                 "| Payload | Version | Result |", "| --- | --- | --- |"]
        lines.extend(f"| {item['name']} | {item.get('version', '?')} | {item['status']} |" for item in report["entries"])
        with open(summary, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    return 1 if report["failed"] else 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="Optional payload name")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--repository", help="Mirror destination OWNER/REPO")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true", help="Upload verified assets, then update catalog")
    mode.add_argument("--download-only", action="store_true", help="Stage files without changing the live catalog")
    mode.add_argument("--dry-run", action="store_true", help="Check versions only (default)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1)
    parser.add_argument("--api-base", default="https://api.github.com", help=argparse.SUPPRESS)
    parser.add_argument("--upload-base", default="https://uploads.github.com", help=argparse.SUPPRESS)
    parser.add_argument("--allow-local-http", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    options.root = options.root.resolve()
    options.api_base = options.api_base.rstrip("/")
    options.upload_base = options.upload_base.rstrip("/")
    if options.retries < 1 or options.timeout <= 0 or options.retry_delay < 0:
        parser.error("retries and timeout must be positive; retry-delay cannot be negative")
    if options.allow_local_http:
        for url in (options.api_base, options.upload_base):
            if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                parser.error("--allow-local-http is restricted to loopback test services")
    elif options.api_base != "https://api.github.com" or options.upload_base != "https://uploads.github.com":
        parser.error("Custom API endpoints are restricted to loopback end-to-end tests")
    return options


def main():
    options = parse_args()
    try:
        return run(options)
    except (ValueError, OSError, HTTPError, URLError, json.JSONDecodeError) as error:
        write_json(options.root / "reports/sync-report.json", {"started_at": utc_now(),
                   "checked": 0, "updated": 0, "unchanged": 0, "failed": 1, "fatal_error": str(error)})
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
