"""Run the actual CLI against a deterministic HTTP service; no unit mocks."""
from __future__ import annotations

import hashlib
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ELF = b"\x7fELF\x02\x01\x01\x00" + b"payload" * 16


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class Service:
    def __init__(self):
        self.releases = {}
        self.files = {}
        self.assets = {}
        self.hits = {}
        self.fail_download = set()
        self.fail_upload = False
        self.exists = False
        self.committed_catalog = []
        self.catalog_reads = 0
        self.change_catalog_after = None
        self.delete_attempts = []
        self.fail_delete = False
        self.release_body = ""
        self.next_id = 100
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, body=b"", **headers):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                service.hits[path] = service.hits.get(path, 0) + 1
                if path == "/repos/test/mirror/releases/tags/payloads-mirror":
                    self.respond(200, {"id": 1, "body": service.release_body}) if service.exists else self.respond(404, {})
                elif path == "/repos/test/mirror/contents/payloads.json":
                    service.catalog_reads += 1
                    data = service.committed_catalog
                    if service.change_catalog_after is not None and service.catalog_reads > service.change_catalog_after:
                        data = []
                    raw = json.dumps(data).encode()
                    self.respond(200, {"encoding": "base64", "content": base64.b64encode(raw).decode(),
                                       "sha": hashlib.sha1(raw).hexdigest()})
                elif path == "/repos/test/mirror/releases/1/assets":
                    page = int(parse_qs(parsed.query).get("page", ["1"])[0])
                    assets = list(service.assets.values())
                    self.respond(200, assets[(page - 1) * 100:page * 100])
                elif path.startswith("/repos/source/") and "/releases" in path:
                    name = path.split("/")[3]
                    releases = service.releases.get(name)
                    if releases is None:
                        return self.respond(503, {"message": "upstream unavailable"})
                    etag = '"' + hashlib.sha256(json.dumps(releases).encode()).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        return self.respond(304, b"")
                    self.respond(200, releases, ETag=etag)
                elif path in service.fail_download:
                    self.respond(503, {"message": "download unavailable"})
                elif path in service.files:
                    self.respond(200, service.files[path])
                else:
                    self.respond(404, {})

            def do_POST(self):
                parsed = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if parsed.path == "/repos/test/mirror/releases":
                    service.exists = True
                    service.release_body = json.loads(body).get("body", "")
                    self.respond(201, {"id": 1, "body": service.release_body})
                elif parsed.path == "/repos/test/mirror/releases/1/assets":
                    if service.fail_upload:
                        return self.respond(503, {"message": "upload unavailable"})
                    name = parse_qs(parsed.query)["name"][0]
                    if name in service.assets:
                        return self.respond(422, {"message": "already_exists"})
                    service.next_id += 1
                    asset = {"id": service.next_id, "name": name, "size": len(body),
                             "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                             "state": "uploaded", "uploader": {"login": "github-actions[bot]"}}
                    service.assets[name] = asset
                    service.files["/mirrored/" + name] = body
                    self.respond(201, asset)
                else:
                    self.respond(404, {})

            def do_DELETE(self):
                prefix = "/repos/test/mirror/releases/assets/"
                if not self.path.startswith(prefix):
                    return self.respond(404, {})
                identity = int(self.path[len(prefix):])
                found = next((asset for asset in service.assets.values() if asset["id"] == identity), None)
                if not found:
                    return self.respond(404, {})
                service.delete_attempts.append(found["name"])
                if service.fail_delete:
                    return self.respond(403, {"message": "deletion forbidden"})
                del service.assets[found["name"]]
                service.files.pop("/mirrored/" + found["name"], None)
                self.respond(204)

            def do_PATCH(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/repos/test/mirror/releases/1":
                    service.release_body = json.loads(body)["body"]
                    self.respond(200, {"id": 1, "body": service.release_body})
                else:
                    self.respond(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def release(self, repo="demo", tag="v1", assets=None, prerelease=False,
                published="2026-10-10T00:00:00Z", draft=False):
        objects = []
        for index, (name, data) in enumerate((assets or {"demo-ps5.elf": ELF}).items()):
            path = f"/files/{repo}/{tag}/{name}"
            self.files[path] = data
            objects.append({"id": len(data) + index, "name": name, "size": len(data),
                            "updated_at": published, "browser_download_url": self.url + path,
                            "digest": "sha256:" + hashlib.sha256(data).hexdigest()})
        return {"id": len(tag), "tag_name": tag, "published_at": published,
                "created_at": published, "draft": draft, "prerelease": prerelease,
                "assets": objects}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run_cli(root, service, expected_code=0, extra=(), mode="--publish"):
    command = [sys.executable, str(PROJECT / "update_payloads.py"), "--root", str(root),
               "--repository", "test/mirror", "--api-base", service.url,
               "--upload-base", service.url, "--allow-local-http", mode,
               "--retries", "2", "--retry-delay", "0", "--timeout", "2", *extra]
    environment = dict(os.environ)
    for key in ("GITHUB_STEP_SUMMARY", "GH_TOKEN", "GITHUB_TOKEN"):
        environment.pop(key, None)
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30,
                            env=environment)
    if result.returncode != expected_code:
        raise AssertionError(f"exit={result.returncode}, expected={expected_code}\n"
                             f"{result.stdout}\n{result.stderr}")
    name = "prune-report.json" if mode in {"--prune", "--plan-prune"} else "sync-report.json"
    report = json.loads((root / "reports" / name).read_text(encoding="utf-8"))
    return report


def write_catalog(root, entries):
    (root / "payloads.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


def catalog(root):
    return json.loads((root / "payloads.json").read_text(encoding="utf-8"))


def entry(name="demo", **extra):
    return {"name": name, "source": f"https://github.com/source/{name}/releases",
            "description": "中文 | description", "category": "Utilities & Tools", **extra}


def main():
    evidence = {"kind": "local-http-end-to-end", "scenarios": [], "passed": False}
    service = Service()
    try:
        scratch = PROJECT / "reports"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mirror-e2e-", dir=scratch) as tmp:
            root = Path(tmp)
            if not root.resolve().is_relative_to(scratch.resolve()):
                raise RuntimeError("End-to-end workspace escaped its expected parent")
            write_catalog(root, [entry(asset_pattern=r"^demo-ps5\.elf$")])
            (root / "README.md").write_text("# 保留正文\n\n<!-- PAYLOADS_START -->\nold\n"
                                            "<!-- PAYLOADS_END -->\n\n结尾保持。\n", encoding="utf-8")
            service.releases["demo"] = [service.release(assets={"demo-ps5.elf": ELF,
                                                               "demo-ps4.elf": ELF + b"ps4",
                                                               "demo-helper.elf": ELF + b"helper"})]
            report = run_cli(root, service)
            current = catalog(root)[0]
            assert report["updated"] == 1 and report["failed"] == 0
            assert current["url"].startswith("https://github.com/test/mirror/")
            assert service.files["/mirrored/" + current["filename"]] == ELF
            text = (root / "README.md").read_text(encoding="utf-8")
            assert "保留正文" in text and "结尾保持。" in text and "\\n|" not in text
            assert "中文 \\| description" in text
            evidence["scenarios"].append("bootstrap, fork URLs, PS5 selection, UTF-8 README")

            hits = sum(count for path, count in service.hits.items() if path.startswith("/files/"))
            previous = (root / "payloads.json").read_bytes()
            report = run_cli(root, service)
            assert report["unchanged"] == 1 and report["updated"] == 0
            assert (root / "payloads.json").read_bytes() == previous
            assert sum(count for path, count in service.hits.items() if path.startswith("/files/")) == hits
            evidence["scenarios"].append("ETag incremental no-op without downloads or metadata changes")

            old_name = current["filename"]
            service.releases["demo"] = [service.release(assets={"demo-ps5.elf": ELF + b"replaced"})]
            report = run_cli(root, service)
            current = catalog(root)[0]
            assert report["updated"] == 1 and current["version"] == "v1"
            assert current["filename"] != old_name and old_name in service.assets
            assert (root / "payloads" / old_name).read_bytes() == ELF
            evidence["scenarios"].append("same-tag replacement uses a new filename and retains old binary")

            del service.assets[current["filename"]]
            report = run_cli(root, service)
            assert report["updated"] == 1 and catalog(root)[0]["filename"] in service.assets
            evidence["scenarios"].append("repair missing release asset")

            previous = (root / "payloads.json").read_bytes()
            new_release = service.release(tag="v2", assets={"demo-ps5.elf": ELF + b"v2"})
            service.releases["demo"] = [new_release]
            service.fail_upload = True
            report = run_cli(root, service, 1)
            assert report["failed"] == 1 and (root / "payloads.json").read_bytes() == previous
            service.fail_upload = False
            report = run_cli(root, service)
            assert report["updated"] == 1 and catalog(root)[0]["version"] == "v2"
            evidence["scenarios"].append("failed upload preserves catalog; subsequent run recovers")

            stable = service.release(tag="v3", published="2026-10-10T01:00:00Z")
            beta = service.release(tag="v4-beta", prerelease=True, published="2026-10-11T01:00:00Z")
            draft = service.release(tag="v5", draft=True, published="2026-10-12T01:00:00Z")
            service.releases["demo"] = [draft, beta, stable]
            run_cli(root, service)
            assert catalog(root)[0]["version"] == "v3"
            items = catalog(root)
            items[0]["include_prereleases"] = True
            write_catalog(root, items)
            run_cli(root, service)
            assert catalog(root)[0]["version"] == "v4-beta"
            evidence["scenarios"].append("stable default, explicit prerelease opt-in, drafts excluded")

            before = catalog(root)[0]
            for mode in ("digest", "size", "empty", "elf", "network"):
                data = b"" if mode == "empty" else (b"bad executable" if mode == "elf" else ELF)
                release = service.release(tag="bad-" + mode, assets={"demo-ps5.elf": data})
                if mode == "digest":
                    release["assets"][0]["digest"] = "sha256:" + "0" * 64
                if mode == "size":
                    release["assets"][0]["size"] += 1
                if mode == "network":
                    service.fail_download.add(urlsplit(release["assets"][0]["browser_download_url"]).path)
                service.releases["demo"] = [release]
                report = run_cli(root, service, 1)
                assert report["failed"] == 1 and catalog(root)[0] == before
                assert (root / "payloads" / before["filename"]).exists()
                evidence["scenarios"].append(f"{mode} failure retains previous usable payload")

            write_catalog(root, [entry("zip", asset_pattern=r"^bundle\.zip$", extract_file="nested/main.elf")])
            service.releases["zip"] = [service.release(repo="zip", assets={
                "bundle.zip": zip_bytes({"nested/main.elf": ELF, "helper.elf": ELF + b"helper"}),
                "wrong.zip": zip_bytes({"wrong.elf": ELF + b"wrong"})})]
            report = run_cli(root, service)
            assert report["updated"] == 1
            assert (root / "payloads" / catalog(root)[0]["filename"]).read_bytes() == ELF
            evidence["scenarios"].append("ZIP respects asset pattern and exact extraction member")

            for mode, members in [("ambiguous", {"a.elf": ELF, "b.elf": ELF}),
                                  ("traversal", {"../a.elf": ELF})]:
                write_catalog(root, [entry("zip", asset_pattern=r"^bundle\.zip$")])
                service.releases["zip"] = [service.release(repo="zip", tag=mode,
                    assets={"bundle.zip": zip_bytes(members)})]
                report = run_cli(root, service, 1)
                assert report["failed"] == 1 and "version" not in catalog(root)[0]
                evidence["scenarios"].append(f"ZIP {mode} rejected without publishing")

            write_catalog(root, [entry("ambiguous")])
            service.releases["ambiguous"] = [service.release(repo="ambiguous",
                assets={"a.elf": ELF, "b.elf": ELF})]
            report = run_cli(root, service, 1)
            assert report["failed"] == 1
            evidence["scenarios"].append("ambiguous release assets require explicit pattern")

            write_catalog(root, [entry("good"), entry("unavailable")])
            service.releases["good"] = [service.release(repo="good", assets={"good.elf": ELF})]
            report = run_cli(root, service, 1)
            assert report["updated"] == 1 and report["failed"] == 1
            assert any(item.get("version") == "v1" for item in catalog(root))
            evidence["scenarios"].append("independent successful updates persist while failure is visible")

            for index in range(105):
                name = f"retained-{index}.elf"
                service.assets[name] = {"id": 1000 + index, "name": name, "size": len(ELF),
                    "digest": "sha256:" + hashlib.sha256(ELF).hexdigest(), "state": "uploaded"}
            report = run_cli(root, service, 1)
            assert report["unchanged"] == 1 and report["failed"] == 1
            evidence["scenarios"].append("mirror asset pagination with more than 100 retained binaries")

            write_catalog(root, [entry("demo")])
            service.releases["demo"] = [service.release(tag="latest", assets={"demo-ps5.elf": ELF + b"latest"})]
            run_cli(root, service)
            current = catalog(root)[0]
            old = [asset for name, asset in service.assets.items() if name.startswith("demo_") and name != current["filename"]]
            assert old
            service.committed_catalog = []
            before_delete = len(service.delete_attempts)
            report = run_cli(root, service, 1, mode="--prune")
            assert report["deleted"] == 0 and len(service.delete_attempts) == before_delete
            evidence["scenarios"].append("cleanup refuses uncommitted catalog before deleting anything")

            service.committed_catalog = catalog(root)
            remote = service.assets[current["filename"]]
            checksum = remote["digest"]
            remote["digest"] = "sha256:" + "0" * 64
            report = run_cli(root, service, 1, mode="--prune")
            assert report["deleted"] == 0 and len(service.delete_attempts) == before_delete
            remote["digest"] = checksum
            evidence["scenarios"].append("cleanup refuses invalid current mirror bytes")

            manual = "demo_manual.elf"
            service.assets[manual] = {"id": 4000, "name": manual, "size": len(ELF),
                "digest": "sha256:" + hashlib.sha256(ELF).hexdigest(), "state": "uploaded", "uploader": {"login": "human"}}
            body_before = service.release_body
            report = run_cli(root, service, mode="--plan-prune")
            assert report["deleted"] == 0 and report["candidate_count"] == len(old)
            assert service.release_body == body_before and len(service.delete_attempts) == before_delete
            assert manual not in {asset["name"] for asset in report["candidates"]}
            evidence["scenarios"].append("cleanup preview preserves manual uploads and unknown assets")

            service.fail_delete = True
            report = run_cli(root, service, 1, mode="--prune")
            assert report["deleted"] == 0 and current["filename"] in service.assets
            service.fail_delete = False
            evidence["scenarios"].append("cleanup deletion failures preserve current files and fail visibly")

            service.catalog_reads = 0
            service.change_catalog_after = 1
            before_delete = len(service.delete_attempts)
            report = run_cli(root, service, 1, mode="--prune")
            assert report["deleted"] == 0 and len(service.delete_attempts) == before_delete
            service.change_catalog_after = None
            evidence["scenarios"].append("cleanup stops if committed catalog changes after planning")

            for index in range(105):
                name = f"demo_old-{index}.elf"
                service.assets[name] = {"id": 5000 + index, "name": name, "size": len(ELF),
                    "digest": "sha256:" + hashlib.sha256(ELF).hexdigest(), "state": "uploaded", "uploader": {"login": "github-actions[bot]"}}
            report = run_cli(root, service, mode="--prune")
            assert report["deleted"] == len(old) + 105
            assert manual in service.assets and current["filename"] in service.assets
            assert not any(name.startswith("demo_") and name not in {manual, current["filename"]} for name in service.assets)
            assert all(f"retained-{index}.elf" in service.assets for index in range(105))
            report = run_cli(root, service, mode="--prune")
            assert report["deleted"] == 0 and report["candidate_count"] == 0
            evidence["scenarios"].append("committed latest-only cleanup handles pagination and repeated runs")
            evidence["passed"] = True
    except Exception as error:
        evidence["error"] = str(error)
        raise
    finally:
        service.close()
        output = PROJECT / "reports/e2e-report.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"E2E evidence: {output}")
    print(f"PASS: {len(evidence['scenarios'])} end-to-end scenarios")


if __name__ == "__main__":
    main()
