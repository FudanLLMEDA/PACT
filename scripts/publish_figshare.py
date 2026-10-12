#!/usr/bin/env python3
"""Publish a new, separately citable v1 archive, retaining the previous record.

Uses FIGSHARE_TOKEN from the environment, never command arguments. Multipart
upload follows figshare/github-upload-action's protocol. By default creates a
draft; --publish is required to make it public. A matching title is resumed,
not duplicated. No existing public article or file is deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.figshare.com/v2"


class Figshare:
    def __init__(self):
        self.token = os.environ["FIGSHARE_TOKEN"]

    def request(self, method, path, payload=None, *, json_response=True):
        url = API + path if path.startswith("/") else path
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.figshare.com":
            raise ValueError("Refusing to send credentials outside the Figshare API")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "token " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
        return json.loads(raw) if raw and json_response else {}

    def find_title(self, title):
        matches = []
        page = 1
        while True:
            rows = self.request("GET", f"/account/articles?page={page}&page_size=100")
            matches.extend(row for row in rows if row["title"] == title)
            if len(rows) < 100:
                break
            page += 1
        if len(matches) > 1:
            raise ValueError("Multiple matching articles; manual selection required")
        return matches[0]["id"] if matches else None

    def upload(self, article_id, path):
        md5 = hashlib.md5()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                md5.update(chunk)
        checksum = md5.hexdigest()
        base = f"/account/articles/{article_id}/files"
        existing = self.request("GET", base)
        candidates = [f for f in existing if f["name"] == path.name]
        if len(candidates) > 1:
            raise ValueError("Duplicate archive filenames in draft")
        if candidates:
            file_id = candidates[0]["id"]
            entry = self.request("GET", f"{base}/{file_id}")
            if entry["size"] != path.stat().st_size or entry.get("supplied_md5") != checksum:
                raise ValueError("Existing filename has different content; refusing to overwrite")
            if entry.get("computed_md5") == checksum:
                print(f"File {file_id}: already uploaded and checksum matches", flush=True)
                return entry
        else:
            created = self.request("POST", base, {"name": path.name, "size": path.stat().st_size, "md5": checksum})
            entry = self.request("GET", created["location"])
            file_id = entry["id"]
        upload_url = entry["upload_url"]
        # The upload URL is a capability URL. No API token is sent to it.
        parsed = urllib.parse.urlparse(upload_url)
        if parsed.scheme != "https" or not parsed.hostname.endswith(".figshare.com"):
            raise ValueError("Unexpected Figshare upload URL")
        with urllib.request.urlopen(upload_url, timeout=120) as response:
            parts = json.load(response)["parts"]
        with path.open("rb") as stream:
            for index, part in enumerate(parts, 1):
                if str(part.get("status", "")).upper() == "COMPLETE":
                    continue
                stream.seek(part["startOffset"])
                data = stream.read(part["endOffset"] - part["startOffset"] + 1)
                for attempt in range(4):
                    try:
                        request = urllib.request.Request(f"{upload_url}/{part['partNo']}", data=data, method="PUT")
                        with urllib.request.urlopen(request, timeout=300) as response:
                            response.read()
                        break
                    except (urllib.error.URLError, TimeoutError):
                        if attempt == 3:
                            raise
                        time.sleep(2 ** attempt)
                print(f"File {file_id}: uploaded part {index}/{len(parts)}", flush=True)
        # Completion may return a successful plain-text/empty acknowledgement.
        self.request("POST", f"{base}/{file_id}", json_response=False)
        for _ in range(30):
            entry = self.request("GET", f"{base}/{file_id}")
            if entry.get("computed_md5") == checksum:
                return entry
            time.sleep(10)
        raise RuntimeError("Figshare has not confirmed the uploaded file checksum")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-article", type=int, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    client = Figshare()
    source = client.request("GET", f"/account/articles/{args.source_article}")
    title = f"PACT: Post-route Agentic Checkpoint Tuning for FPGA Timing Closure — FPT 2026 AE {args.version}"
    article_id = client.find_title(title)
    if article_id is None:
        description = (
            f"<p>FPT 2026 paper artifact maintenance release {html.escape(args.version)}. "
            "This is the v1 paper artifact, not the v2 FPL contest artifact.</p>"
            "<p>The ZIP includes PACT source, 35 original benchmark DCPs, pinned RapidWright "
            "source and nested submodules, baseline runners, setup documentation, result "
            "export/aggregation scripts, tests and a per-file integrity manifest. "
            "Vivado, its license and prebuilt Python/Java environments are not included.</p>"
            "<p>This release updates the README and Codex setup, clarifies DATuner's "
            "3,600-second per-trial timeout, adds machine-readable PACT results and aggregation, "
            "restores original timing constraints before candidate scoring/export, and adds "
            "common route/hold/pulse/replay checks plus default final structural/simulation checks. "
            "Sampled simulation is not formal equivalence; skipped checks are not passes.</p>"
            "<p>This maintenance update fixes DATuner path resolution, multiline Tcl response "
            "framing, project-state/timeout recovery and MCP 1.x compatibility; it also adds "
            "documented runtime controls and compiler/elaborator failure classification.</p>"
            "<p>Real Vivado 2025.1 runs produced validated optimized outputs for AMD Mini-ISP, "
            "Double-Clock FFT-L and Stress FFT-CORDIC-M. A BOOM candidate measured 89.654 MHz "
            "after original-constraint restoration and passed independent structural and "
            "50/200-cycle sampled functional checks at 64 GiB RAM. That candidate was recovered "
            "after an optimization timeout, not a one-hour/32 GB end-to-end success. "
            "Selected measured evidence and runtime guidance are included. Unit tests and "
            "35-design dry-runs passed; historical paper results have not been regenerated, "
            "and these checks do not constitute full-suite replication or formal equivalence.</p>"
            f'<p>Source and release: <a href="{html.escape(args.release_url)}">GitHub release</a>. '
            f'Previous archive: <a href="https://doi.org/10.6084/m9.figshare.{args.source_article}">'
            "original Figshare record</a>. The previous record is retained unchanged.</p>"
            "<p>Licensing follows the included LICENSE files: PACT-authored code is MIT; "
            "AMD/third-party components retain their own licenses.</p>"
        )
        body = {"title": title, "description": description,
                "defined_type": "software", "license": source["license"]["value"] if "value" in source["license"] else source["license"]["id"],
                "authors": [{"id": a["id"]} for a in source["authors"]],
                "categories": [c["id"] for c in source["categories"]],
                "tags": list(dict.fromkeys(source.get("tags", []) + ["PACT", "FPGA", "FPT 2026", "artifact evaluation", args.version])),
                "references": [args.release_url, f"https://doi.org/10.6084/m9.figshare.{args.source_article}"]}
        created = client.request("POST", "/account/articles", body)
        article_id = int(created["location"].rstrip("/").split("/")[-1])
        print(f"Created new draft article {article_id}; previous article unchanged", flush=True)
    else:
        print(f"Resuming article {article_id}", flush=True)
    file_info = client.upload(article_id, args.archive)
    checksum_path = args.archive.with_suffix(".zip.sha256")
    if checksum_path.is_file():
        client.upload(article_id, checksum_path)
    article = client.request("GET", f"/account/articles/{article_id}")
    if args.publish and not article.get("is_public"):
        client.request("POST", f"/account/articles/{article_id}/publish", json_response=False)
    if args.publish:
        for attempt in range(12):
            try:
                # Public endpoint, independent of account draft state.
                with urllib.request.urlopen(f"{API}/articles/{article_id}", timeout=120) as response:
                    article = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404 or attempt == 11:
                    raise
                time.sleep(10)
        files = article.get("files", [])
        if not any(f["id"] == file_info["id"] and f.get("computed_md5") == file_info["computed_md5"] for f in files):
            raise RuntimeError("Public article does not contain the verified archive")
    result = {k: article.get(k) for k in ("id", "title", "doi", "version", "url_public_html", "url_public_api")}
    result["published"] = args.publish
    result["archive"] = {k: file_info.get(k) for k in ("id", "name", "size", "computed_md5", "download_url")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        # Never log request headers, capability upload URLs or API tokens.
        raise SystemExit(f"Figshare API returned HTTP {exc.code}; inspect draft/account state before retrying")
