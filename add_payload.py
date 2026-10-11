"""Register an upstream source; the mirror publishes it after verification."""
import argparse
from pathlib import Path
import re
from urllib.parse import urlsplit

from update_payloads import load_json, repository_name, update_readme, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="GitHub/Gitea repository URL")
    parser.add_argument("--name", help="Unique display name")
    parser.add_argument("--asset-pattern", help="Regex matching the intended release asset")
    parser.add_argument("--extract-file", help="Exact ELF/bin member inside a ZIP")
    parser.add_argument("--include-prereleases", action="store_true")
    parser.add_argument("--description", default="")
    parser.add_argument("--category", default="Utilities & Tools")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--repository")
    args = parser.parse_args()
    source = args.source or input("GitHub/Gitea repository URL: ").strip()
    parts = urlsplit(source)
    path = parts.path.strip("/").split("/")
    if parts.scheme != "https" or not parts.netloc or len(path) < 2 or parts.username or parts.password:
        parser.error("Use an HTTPS repository URL without embedded credentials")
    name = args.name or input(f"Payload name [{path[1]}]: ").strip() or path[1]
    if args.asset_pattern:
        try:
            re.compile(args.asset_pattern)
        except re.error as error:
            parser.error(str(error))
    repository = repository_name(args.repository)
    items = load_json(args.root / "payloads.json", [])
    if any(item["name"] == name for item in items):
        parser.error(f"Payload name already exists: {name}")
    item = {"name": name, "source": f"{parts.scheme}://{parts.netloc}/{path[0]}/{path[1].removesuffix('.git')}/releases",
            "description": args.description, "category": args.category}
    if args.asset_pattern:
        item["asset_pattern"] = args.asset_pattern
    if args.extract_file:
        item["extract_file"] = args.extract_file
    if args.include_prereleases:
        item["include_prereleases"] = True
    items.append(item)
    write_json(args.root / "payloads.json", items)
    update_readme(args.root, repository, items)
    print(f"Registered {name}. The next mirror run will verify and publish its latest release.")


if __name__ == "__main__":
    main()
