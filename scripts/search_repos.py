#!/usr/bin/env python3
"""Collect public GitHub projects, then check README relevance before shortlisting."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

CONFIG = Path(__file__).resolve().parents[1] / "config.json"


class ApiError(Exception):
    def __init__(self, message, stop=False, status=None):
        super().__init__(message)
        self.stop = stop
        self.status = status


def resolve_config(args, parser):
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    settings = dict(config["defaults"])
    preset = args.preset or settings.get("preset", "balanced")
    if preset not in config["presets"]:
        parser.error("Unknown preset: " + preset)
    settings.update(config["presets"][preset])
    settings["preset"] = preset
    for key in settings:
        value = getattr(args, key, None)
        if value is not None:
            settings[key] = value
    for key in ("min_stars", "active_days", "created_days", "readme_budget"):
        if type(settings[key]) is not int or settings[key] < 0:
            parser.error(key + " must be a nonnegative integer")
    maximum = settings["max_stars"]
    if maximum is not None and (type(maximum) is not int or maximum < settings["min_stars"]):
        parser.error("max_stars must be an integer >= min_stars")
    for key in ("limit", "per_query"):
        if type(settings[key]) is not int or not 1 <= settings[key] <= 1000:
            parser.error(key + " must be between 1 and 1000")
    for key in ("queries", "topics"):
        if not isinstance(settings[key], list) or not all(isinstance(x, str) for x in settings[key]):
            parser.error(key + " must be a list of strings")
    for key in ("include_forks", "include_archived"):
        if type(settings[key]) is not bool:
            parser.error(key + " must be true or false")
    for key in ("language", "license"):
        if settings[key] is not None and not isinstance(settings[key], str):
            parser.error(key + " must be a string or null")
    if settings["sort"] not in ("stars", "updated", "best-match"):
        parser.error("sort must be stars, updated, or best-match")
    if settings["category"] not in config["categories"] and not settings["queries"]:
        parser.error("Unknown category; use --query and a relevance profile for a custom type")
    seeds = settings["queries"] or config["categories"][settings["category"]]
    if not isinstance(seeds, list) or not seeds or not all(isinstance(x, str) for x in seeds):
        parser.error("category seeds must be a nonempty list of strings")
    profiles = config.get("relevance_profiles", {})
    selected = settings["profile"]
    if selected == "auto":
        selected = settings["category"] if settings["category"] in profiles else None
    if selected is not None and selected not in profiles:
        parser.error("Unknown relevance profile: " + str(selected))
    settings["profile"] = selected
    profile = profiles.get(selected)
    if profile:
        for key in ("direct_groups", "readme_groups"):
            groups = profile[key]
            if not isinstance(groups, list) or not groups or any(
                not isinstance(group, list) or not group or any(not isinstance(term, str) or not term.strip() for term in group)
                for group in groups
            ):
                parser.error(key + " must contain nonempty groups of terms")
        for key in ("exclude_metadata", "review_only_metadata", "adjacent_phrases"):
            if not isinstance(profile.get(key, []), list) or any(not isinstance(term, str) or not term.strip() for term in profile.get(key, [])):
                parser.error(key + " must be a list of nonempty terms")
    return settings, seeds, profile


def quoted(value):
    if not value.strip() or any(c in value for c in ('"', '\n', '\r')):
        raise ValueError("Qualifier values must be nonempty and cannot contain quotes or newlines")
    return '"' + value + '"' if any(c.isspace() for c in value) else value


def make_queries(settings, seeds, as_of):
    maximum = settings["max_stars"]
    stars = (str(settings["min_stars"]) + ".." + str(maximum)
             if maximum is not None else ">=" + str(settings["min_stars"]))
    qualifiers = ["is:public", "stars:" + stars]
    if not settings["include_archived"]:
        qualifiers.append("archived:false")
    if settings["include_forks"]:
        qualifiers.append("fork:true")
    for key, qualifier in (("active_days", "pushed"), ("created_days", "created")):
        if settings[key]:
            qualifiers.append(qualifier + ":>=" + (as_of - timedelta(days=settings[key])).isoformat())
    for key in ("language", "license"):
        if settings[key]:
            qualifiers.append(key + ":" + quoted(settings[key]))
    qualifiers.extend("topic:" + quoted(topic) for topic in settings["topics"])
    return list(dict.fromkeys(" ".join([seed.strip(), *qualifiers]).strip() for seed in seeds))


def existing_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and token.strip():
        return token.strip()
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run([gh, "auth", "token", "--hostname", "github.com"],
                                    capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                return result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def api_get(path, token, params=None):
    url = "https://api.github.com/" + path
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "github-scout-skill",
               "X-GitHub-Api-Version": "2026-03-10"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ApiError("GitHub response exceeds 8MB")
            return json.loads(raw)
    except HTTPError as exc:
        recovery = [header + "=" + exc.headers[header] for header in
                    ("Retry-After", "X-RateLimit-Reset", "X-RateLimit-Remaining") if header in exc.headers]
        raise ApiError("GitHub HTTP " + str(exc.code) + ("; " + "; ".join(recovery) if recovery else ""),
                       stop=exc.code in (401, 403, 429), status=exc.code) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ApiError("GitHub request failed (" + type(exc).__name__ + ")") from None


def fetch(query, page, per_page, sort, token):
    params = {"q": query, "per_page": per_page, "page": page}
    if sort != "best-match":
        params.update(sort=sort, order="desc")
    return api_get("search/repositories", token, params)


def fetch_readme(full_name, token):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
        raise ApiError("Invalid repository name")
    data = api_get("repos/" + quote(full_name, safe="/") + "/readme", token)
    if data.get("encoding") != "base64" or not data.get("content"):
        raise ApiError("README content unavailable; manual review needed")
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        raise ApiError("README decoding failed") from None
    return {"text": content, "url": data.get("html_url"), "sha": data.get("sha")}


def seen_names(path):
    if not path:
        return set()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("raw_repositories", payload.get("repositories"))
    if not isinstance(payload, list):
        raise ValueError("--seen needs a prior output or a JSON array of repository names")
    names = set()
    for entry in payload:
        name = entry.get("full_name") if isinstance(entry, dict) else entry
        if not isinstance(name, str) or name.count("/") != 1:
            raise ValueError("Invalid repository name in --seen")
        names.add(name.casefold())
    return names


def eligible(repo, settings, as_of):
    if repo.get("private") or repo.get("disabled"):
        return False
    if (not settings["include_archived"] and repo.get("archived")) or (not settings["include_forks"] and repo.get("fork")):
        return False
    stars = repo.get("stargazers_count", 0)
    if stars < settings["min_stars"] or (settings["max_stars"] is not None and stars > settings["max_stars"]):
        return False
    if settings["language"] and (repo.get("language") or "").casefold() != settings["language"].casefold():
        return False
    if settings["license"]:
        info = repo.get("license") or {}
        if settings["license"].casefold() not in {(info.get("key") or "").casefold(), (info.get("spdx_id") or "").casefold()}:
            return False
    topics = {t.casefold() for t in repo.get("topics", [])}
    if any(t.casefold() not in topics for t in settings["topics"]):
        return False
    for key, field in (("active_days", "pushed_at"), ("created_days", "created_at")):
        if settings[key]:
            try:
                actual = date.fromisoformat((repo.get(field) or "")[:10])
            except ValueError:
                return False
            if actual < as_of - timedelta(days=settings[key]):
                return False
    return True


def normalize(repo):
    info = repo.get("license") or {}
    keys = ("id", "full_name", "html_url", "description", "language", "topics", "homepage",
            "stargazers_count", "forks_count", "created_at", "pushed_at", "updated_at", "archived", "fork")
    result = {key: repo.get(key) for key in keys}
    result.update(license=info.get("spdx_id") or info.get("key") or "UNKNOWN", license_url=info.get("url"),
                  readme_url=str(repo["html_url"]) + "#readme", matched_queries=[])
    return result


def collect(settings, queries, as_of, token, seen):
    found, scans, warnings = {}, [], []
    partial = stopped = False
    for query in queries:
        scan = {"query": query, "total_count": None, "fetched": 0,
                "incomplete_results": False, "truncated": False, "error": None}
        scans.append(scan)
        per_page = min(100, settings["per_query"])
        if stopped:
            scan["error"] = "Skipped after authentication or rate-limit failure"
        else:
            for page in range(1, (settings["per_query"] + per_page - 1) // per_page + 1):
                try:
                    data = fetch(query, page, per_page, settings["sort"], token)
                except ApiError as exc:
                    scan["error"], partial, stopped = str(exc), True, exc.stop
                    break
                scan["total_count"] = data.get("total_count", 0)
                scan["incomplete_results"] |= bool(data.get("incomplete_results"))
                partial |= scan["incomplete_results"]
                items = data.get("items", [])
                for repo in items[:settings["per_query"] - scan["fetched"]]:
                    scan["fetched"] += 1
                    if repo["full_name"].casefold() in seen or not eligible(repo, settings, as_of):
                        continue
                    if repo["id"] not in found:
                        found[repo["id"]] = normalize(repo)
                    if query not in found[repo["id"]]["matched_queries"]:
                        found[repo["id"]]["matched_queries"].append(query)
                if not items or scan["fetched"] >= min(scan["total_count"], settings["per_query"]):
                    break
        if scan["total_count"] is not None and scan["total_count"] > scan["fetched"]:
            scan["truncated"] = True
            warnings.append("Candidate budget/API cap reached for query: " + query)
        if scan["incomplete_results"]:
            warnings.append("GitHub marked query incomplete: " + query)
        if scan["error"]:
            warnings.append(scan["error"] + ": " + query)
    repositories = list(found.values())
    sort_repositories(repositories, settings["sort"])
    return {"status": "partial" if partial else "ok", "queries": scans, "warnings": warnings,
            "requests_stopped": stopped, "repositories": repositories}


def sort_repositories(repositories, sort):
    if sort in ("stars", "updated"):
        key = "stargazers_count" if sort == "stars" else "updated_at"
        repositories.sort(key=lambda item: item.get(key) or (0 if sort == "stars" else ""), reverse=True)


def clean_document(text):
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"```.*?```|~~~.*?~~~", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\s)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>|https?://\S+|data:image/\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def term_match(text, term):
    # English word boundaries keep 'cat' from matching 'category'; CJK phrases stay literal.
    boundary = r"(?<![a-z0-9]){}(?![a-z0-9])" if term.isascii() else "{}"
    return re.search(boundary.format(re.escape(term.casefold())), text.casefold())


def group_hits(text, groups):
    hits = [[term for term in group if term_match(text, term)] for group in groups]
    return hits if all(hits) else None


def primary_text(repo):
    return clean_document(repo["full_name"] + " " + (repo.get("description") or "")[:1200])


def metadata_rejection(repo, profile):
    if profile:
        text = primary_text(repo)
        for term in profile.get("exclude_metadata", []):
            if term_match(text, term):
                return "Primary repository purpose outside scope: " + term
    return None


def metadata_review_reason(repo, profile):
    if profile:
        for term in profile.get("review_only_metadata", []):
            if term_match(primary_text(repo), term):
                return "Broader framework or reference collection; scoped semantic review needed: " + term
    return None


def classify_relevance(repo, readme, profile):
    """A conservative lexical gate; passing this is not a semantic/quality endorsement."""
    if metadata_rejection(repo, profile):
        return {"status": "rejected", "reason": metadata_rejection(repo, profile), "evidence": []}
    if metadata_review_reason(repo, profile):
        return {"status": "needs_review", "reason": metadata_review_reason(repo, profile), "evidence": []}
    body = clean_document(readme[:200_000])
    if not body or not profile:
        return {"status": "needs_review", "reason": "No readable README or no scoped profile; semantic review required", "evidence": []}
    # Require purpose signals in the repository identity or opening, not a random later example.
    introduction = body[:1800]
    identity = primary_text(repo)
    purpose = group_hits(identity, profile["direct_groups"]) or group_hits(introduction[:600], profile["direct_groups"])
    # README evidence must co-occur in a short passage, not in unrelated sections of a long file.
    evidence = []
    for offset in range(0, min(len(body), 60_000), 240):
        window = body[offset:offset + 480]
        hits = group_hits(window, profile["readme_groups"])
        if hits:
            evidence.append({"source": "readme", "text": window, "matched_terms": hits})
            break
    if purpose and evidence:
        return {"status": "direct", "reason": "Project purpose and README passage match the scoped concepts", "evidence": evidence}
    for phrase in profile.get("adjacent_phrases", []):
        # A general tool is eligible as adjacent only when its own README describes that capability.
        match = term_match(introduction, phrase)
        if match:
            return {"status": "adjacent", "reason": "General tool with relevant capability; application needs verification",
                    "evidence": [{"source": "readme", "text": introduction[max(0, match.start()-100):match.end()+260], "matched_terms": [[phrase]]}]}
    return {"status": "needs_review", "reason": "Insufficient purpose/evidence alignment; do not auto-shortlist", "evidence": []}


def review_candidates(result, settings, profile, token, as_of):
    raw = result["repositories"]
    def review_priority(repo):
        if metadata_rejection(repo, profile):
            return 4
        if metadata_review_reason(repo, profile):
            return 3
        if profile and group_hits(primary_text(repo), profile["direct_groups"]):
            return 0
        if profile and any(term_match(primary_text(repo), term) for term in profile.get("adjacent_phrases", [])):
            return 1
        return 2

    queue = sorted(raw, key=review_priority)
    accepted, rejected, pending = [], [], []
    remaining = settings["readme_budget"]
    stopped = result.get("requests_stopped", False)
    for source in queue:
        repo = dict(source)
        reason = metadata_rejection(repo, profile)
        if reason:
            repo["relevance"] = {"status": "rejected", "reason": reason, "evidence": []}
            rejected.append(repo)
            continue
        review_reason = metadata_review_reason(repo, profile)
        if review_reason:
            repo["relevance"] = {"status": "needs_review", "reason": review_reason, "evidence": []}
            pending.append(repo)
            continue
        if stopped or remaining == 0:
            repo["relevance"] = {"status": "needs_review", "reason": "README budget exhausted or requests stopped", "evidence": []}
            pending.append(repo)
            continue
        remaining -= 1
        try:
            readme = fetch_readme(repo["full_name"], token)
            repo["readme_url"] = readme["url"] or repo["readme_url"]
            repo["readme_sha"] = readme.get("sha")
            repo["readme_excerpt"] = clean_document(readme["text"][:200_000])[:12_000]
            repo["relevance"] = classify_relevance(repo, readme["text"], profile)
        except ApiError as exc:
            repo["relevance"] = {"status": "needs_review", "reason": str(exc), "evidence": []}
            if exc.status != 404:
                result["status"] = "partial"
                result["warnings"].append("README check: " + str(exc) + ": " + repo["full_name"])
            stopped |= exc.stop
        try:
            age = (as_of - date.fromisoformat((repo.get("pushed_at") or "")[:10])).days
            repo["activity"] = {"days_since_push": max(0, age), "label": "recent" if age <= 180 else "older-push-review-compatibility"}
        except ValueError:
            repo["activity"] = {"days_since_push": None, "label": "unknown"}
        status = repo["relevance"]["status"]
        (accepted if status in ("direct", "adjacent") else rejected if status == "rejected" else pending).append(repo)
    # Stable sort: relevance precedes stars/updated/best-match; recency is informational by default.
    sort_repositories(accepted, settings["sort"])
    accepted.sort(key=lambda item: 0 if item["relevance"]["status"] == "direct" else 1)
    result.update(raw_repositories=raw, repositories=accepted, shortlist=accepted[:settings["limit"]],
                  rejected=rejected, review_queue=pending,
                  review_summary={"raw": len(raw), "accepted": len(accepted), "rejected": len(rejected),
                                  "needs_review": len(pending), "readme_requests": settings["readme_budget"] - remaining})
    if pending:
        result["warnings"].append("Some candidates require semantic/README review; they were not auto-shortlisted")
    if len(accepted) < settings["limit"]:
        result["warnings"].append("Fewer checked candidates than requested; hard filters preserved")
    return result


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--preset", help="balanced, established, emerging, or a configured preset")
    parser.add_argument("--category", help="A configured category, including animal-modeling and game-ui")
    parser.add_argument("--profile", help="auto, animal-modeling, game-ui, or a configured relevance profile")
    for flag in ("min-stars", "max-stars", "active-days", "created-days", "limit", "per-query", "readme-budget"):
        parser.add_argument("--" + flag, type=int)
    parser.add_argument("--language")
    parser.add_argument("--license", help="Exact SPDX ID or GitHub key, e.g. mit or apache-2.0")
    parser.add_argument("--topic", action="append", dest="topics")
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument("--include-forks", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--include-archived", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--sort", choices=("stars", "updated", "best-match"))
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--seen", type=Path)
    parser.add_argument("--output", type=Path, help="Write UTF-8 JSON here; otherwise print JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview queries/config without networking")
    args = parser.parse_args(argv)
    try:
        settings, seeds, profile = resolve_config(args, parser)
        queries = make_queries(settings, seeds, args.as_of)
        seen = seen_names(args.seen)
        result = {"schema_version": 2, "as_of": args.as_of.isoformat(), "settings": settings}
        if args.dry_run:
            result.update(status="dry-run", queries=queries, seen_count=len(seen))
        else:
            token = existing_token()
            result.update(retrieved_at=datetime.now(timezone.utc).isoformat(), authentication="existing" if token else "anonymous")
            result.update(review_candidates(collect(settings, queries, args.as_of, token, seen), settings, profile, token, args.as_of))
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                              "review_summary": result.get("review_summary"), "warnings": result.get("warnings", [])}, ensure_ascii=False))
        else:
            print(serialized)
        return 2 if result["status"] == "partial" else 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
