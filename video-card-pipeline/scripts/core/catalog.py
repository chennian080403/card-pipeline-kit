# card-core copy -- do not edit here: edit card-core/core (next to this skill), then run card-core/sync.py
"""A deterministic, read-only projection of pipeline records."""

import json
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import yaml

from . import naming, sources
from .jsonio import atomic_write
from .taxonomy import TaxonomyError

MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
              ".mp4", ".mov", ".webm", ".mkv", ".wav", ".mp3"}


def _path(root, value):
    return Path(os.path.abspath(root / Path(str(value)).expanduser()))


def _symlink(path):
    return any(part.is_symlink() for part in (path, *path.parents))


def _files(path, suffixes, recursive=False):
    if _symlink(path) or not path.is_dir():
        return
    if recursive:
        for folder, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink())
            for name in sorted(names):
                child = Path(folder) / name
                if child.suffix.lower() in suffixes and not child.is_symlink() and child.is_file():
                    yield child
    else:
        for child in sorted(path.iterdir()):
            if not child.is_symlink() and child.suffix.lower() in suffixes and child.is_file():
                yield child


def _read(path, problems, *, optional=False):
    if _symlink(path) or optional and not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        raise ValueError("expected a mapping")
    except (OSError, ValueError, yaml.YAMLError) as error:
        problems.append({"path": str(path), "message": f"cannot read record: {error}"})
        return {}


def _top(pack):
    if isinstance(pack, (list, tuple)):
        return str(pack[0]) if pack else ""
    return str(pack or "").strip().replace("\\", "/").split("/", 1)[0]


def build_catalog(cfg, taxonomy, extra_providers=()):
    """Scan supported records without mutating or following directory links."""
    root = Path(cfg.root)
    raw = cfg.raw
    entries, scan_problems = {}, []
    kinds = cfg.kinds if hasattr(type(cfg), "kinds") else taxonomy.to_sources_kinds()
    policy = naming.policy_for(taxonomy.vocab, raw)
    video_policy = {**policy, **((raw.get("video") or {}).get("naming") or {})}
    evaluation = next(iter(taxonomy.categories("evaluation")), "")
    deliverable = next(iter(taxonomy.categories("deliverable")), "")
    reference = taxonomy.default_reference_category()

    def relative(path):
        return os.path.relpath(path, root)

    def add(kind, path=None, *, entry_key=None, url="", **fields):
        if path is not None and _symlink(path):
            return
        file = relative(path) if path is not None else url
        key = f"{kind}:{entry_key or file}"
        token = fields.get("category") or ""
        cid = taxonomy.resolve(token)
        entries[key] = {"key": key, "kind": kind, "file": file,
                        "name": path.name if path is not None else fields.get("title", url),
                        "exists": path.exists() if path is not None else bool(url),
                        "category_id": cid, "category_label": taxonomy.label(cid), **fields}

    def linked_path(base, value):
        return _path(base, value) if value and not str(value).startswith(("http://", "https://")) else None

    image_paths = raw.get("paths") or {}
    video = raw.get("video") or {}
    layouts = []
    if not hasattr(cfg, "video") or image_paths.get("cards") or raw.get("providers"):
        layouts.append(("image", image_paths, "cards", "output"))
    if hasattr(cfg, "video") or video:
        layouts.append(("video", video.get("paths") or {}, "video-cards", "video-output"))
    cards = {}
    for medium, paths, default_cards, _ in layouts:
        for path in _files(_path(root, paths.get("cards", default_cards)), {".yaml", ".yml"}):
            if path.name.startswith("_"):
                continue
            data = _read(path, scan_problems)
            card = str(data.get("id") or path.stem)
            token = _top(data.get("pack"))
            if not token:
                token = next((spec["label"] for section, spec in kinds.items()
                              if not spec["text"] and sources.card_names(data, section)), "")
            if not token and taxonomy.enabled:
                token = deliverable or next(iter(taxonomy.categories()), "")
            elif not token:
                token = taxonomy.resolve("测试") or next(iter(taxonomy.categories()), "")
            cards.setdefault((medium, card), (token, data.get("schema_version")))
            add("card", path, medium=medium, card_id=card, schema_version=data.get("schema_version"),
                title=str(data.get("title") or card), category=token, pack=data.get("pack"),
                tags=data.get("tags") or [])

    def media_fields(name, medium, card=None, version=None, model=None, seq=None):
        try:
            parsed = naming.parse_media_name(name, policy=video_policy if medium == "video" else policy)
        except naming.NamingError:
            parsed = {}
        if not card:
            card = parsed.get("card_id") or next((
                cid for m, cid in sorted(cards, key=lambda item: -len(item[1]))
                if m == medium and name.startswith(f"{cid}_")), "")
        token, schema = cards.get((medium, card), ("", None))
        return dict(medium=medium, card_id=card, card_exists=(medium, card) in cards,
                    legacy_card=(medium, card) in cards and schema != 2, category=token,
                    model=model or parsed.get("model_code"), seq=seq, naming_version=version)

    for medium, paths, _, default_output in layouts:
        out = _path(root, paths.get("output", default_output))
        records = _read(out / "manifest.json", scan_problems, optional=True)
        found = set()
        if medium == "image":
            for key, job in sorted((records.get("jobs") or {}).items()):
                if not isinstance(job, dict) or not job.get("candidate"):
                    continue
                parts = str(key).split("|")
                path = _path(out, job["candidate"])
                found.add(path)
                add("candidate", path, entry_key=key, **media_fields(
                    path.name, medium, job.get("card_id") or parts[0], job.get("naming_version"),
                    job.get("canonical_model") or job.get("model_alias") or (parts[1] if len(parts) == 3 else None),
                    job.get("seq")))
            for path in _files(out / "candidates", MEDIA_EXTS):
                if path not in found:
                    add("candidate", path, **media_fields(path.name, medium))
        else:
            manifest_takes = {}
            for key, job in sorted((records.get("jobs") or {}).items()):
                if isinstance(job, dict) and job.get("take"):
                    manifest_takes[str(job["take"])] = (key, job)
            for sidecar in _files(out / "takes", {".json"}):
                data = _read(sidecar, scan_problems)
                path = _path(root, data["file"]) if data.get("file") else sidecar.with_suffix(".mp4")
                found.add(path)
                _, job = manifest_takes.pop(sidecar.stem, ("", {}))
                derived = data.get("derived_from")
                add("take", path, entry_key=relative(sidecar), title=str(data.get("title") or sidecar.stem),
                    derived_from=derived, sample_name=f"{derived}.mp4" if derived else None,
                    **media_fields(path.name, medium, data.get("card_id") or data.get("card") or job.get("card"),
                                   data.get("naming_version") or job.get("naming_version"),
                                   data.get("canonical_model") or job.get("canonical_model") or data.get("model"),
                                   data.get("seq") or job.get("seq")))
            for path in _files(out / "takes", MEDIA_EXTS):
                if path not in found and path.stem not in manifest_takes:
                    add("take", path, **media_fields(path.name, medium))
            for name, (key, job) in manifest_takes.items():
                path = out / "takes" / f"{name}.mp4"
                if _symlink(path.with_suffix(".json")):
                    continue
                add("take", path, entry_key=key, missing_record=True, **media_fields(
                    path.name, medium, job.get("card_id") or job.get("card"),
                    job.get("naming_version"), job.get("canonical_model") or job.get("model"), job.get("seq")))
        for path in _files(out, {".html"}, recursive=True):
            add("evaluation", path, category=evaluation or "评测")

    sources_path = _path(root, image_paths.get("sources") or (video.get("paths") or {}).get("sources") or "sources.yaml")
    registry = _read(sources_path, scan_problems, optional=True)
    for section, records in sorted(registry.items(), key=lambda item: str(item[0])):
        if not isinstance(records, dict):
            continue
        for name, data in sorted(records.items(), key=lambda item: str(item[0])):
            if not isinstance(data, dict):
                continue
            token = _top(data.get("pack")) or next(
                (cid for cid, spec in taxonomy.categories("source").items()
                 if spec["source_meta"].get("section") == section), "")
            token = token or (kinds.get(section) or {}).get("label") or str(section)
            source = data.get("source", "")
            add("source", linked_path(root, source), entry_key=f"{section}/{name}",
                url=str(source) if str(source).startswith(("http://", "https://")) else "",
                title=str(name), category=token, schema_version=data.get("schema_version"),
                category_role="source")

    inspiration = _path(root, image_paths.get("inspiration", "inspiration"))
    index_path = inspiration / "index.json"
    reference_items = _read(index_path, scan_problems, optional=True)
    indexed = set()
    for key, data in sorted((reference_items.get("items") or {}).items()):
        if not isinstance(data, dict):
            continue
        path = linked_path(inspiration, data.get("file"))
        if path is not None:
            indexed.add(path)
        add("reference", path, entry_key=key, title=str(data.get("title") or key), tags=data.get("tags") or [],
            category=_top(data.get("pack")) or reference, category_role="reference",
            schema_version=data.get("schema_version"))
    for path in _files(inspiration, MEDIA_EXTS, recursive=True):
        if path not in indexed:
            parts = path.relative_to(inspiration).parts
            add("reference", path, category=parts[0] if len(parts) > 1 else reference, category_role="reference")

    spec = cfg.library
    if spec.get("kind") == "local" and spec.get("root"):
        configured_root = (raw.get("library") or {}).get("root") or spec["root"]
        library_root = _path(root, configured_root)
        lib = _read(library_root / "library.json", scan_problems, optional=True)
        for pos, data in enumerate(lib.get("entries") or []):
            if not isinstance(data, dict):
                continue
            path = linked_path(library_root, data.get("file"))
            owner = data.get("card_id") or str(data.get("title") or "").partition(" ")[0]
            token, _ = cards.get(("image", owner), cards.get(("video", owner), ("", None)))
            candidate = data.get("candidate_name")
            add("library", path, entry_key=str(data.get("id") or pos), title=str(data.get("title") or ""),
                category=data.get("category_id") or data.get("storage_category") or token, card_id=owner,
                model=data.get("model"), candidate_name=candidate, sample_name=candidate,
                medium="image" if candidate else "", naming_version=data.get("naming_version"),
                legacy_card=bool(candidate) and cards.get(("image", owner), ("", None))[1] != 2,
                pack=data.get("pack_name") or data.get("batch"), tags=data.get("tags") or [])

    options = raw.get("catalog") or {}
    seen_external = set()
    for spec in options.get("external_roots") or []:
        if not isinstance(spec, dict) or not spec.get("path") or spec.get("depth", 1) != 1:
            raise TaxonomyError("catalog.external_roots entries need path and depth: 1")
        path = _path(root, spec["path"])
        if _symlink(path):
            continue
        if not path.is_dir():
            add("external", path, category=spec.get("category") or "")
            continue
        for child in sorted(path.iterdir()):
            if child.name.startswith(".") or child.is_symlink() or child in seen_external:
                continue
            seen_external.add(child)
            add("external", child, category=spec.get("category") or "")

    for provider in extra_providers:
        for record in provider(cfg, taxonomy):
            data = dict(record)
            add(data.pop("kind", "external"), linked_path(root, data.pop("file", None)), **data)
    return {"version": 1, "entries": sorted(entries.values(), key=lambda e: e["key"]),
            "policy": policy, "video_policy": video_policy, "scan_problems": scan_problems,
            "migrations": taxonomy.migration_report(e["category"] for e in entries.values() if e["category"])}


def render_markdown(catalog):
    def esc(value):
        return re.sub(r"([\\\`\[\]<>*_])", r"\\\1", str(value)).replace("\n", " ")

    groups = defaultdict(list)
    for row in catalog["entries"]:
        groups[row["category_label"]].append(row)
    lines = ["# 素材台账", "", f"共 {len(catalog['entries'])} 项。"]
    for group, rows in sorted(groups.items()):
        lines.extend(["", f"## {esc(group)}（{len(rows)}）", ""])
        for row in rows:
            title = esc(row.get("title") or row["name"])
            destination = quote(row["file"], safe="/:?#=&%" if row["file"].startswith(("http://", "https://")) else "/")
            link = f"[{title}]({destination})" if destination else title
            lines.append(f"- {link} · {row['kind']}" + (f" · {esc(row['model'])}" if row.get("model") else ""))
    return "\n".join(lines) + "\n"


def check_catalog(catalog, taxonomy):
    """Return diagnostics; only versioned records turn a violation into an error."""
    result = [{"level": "warning", "key": p["path"], "message": p["message"]}
              for p in catalog.get("scan_problems", [])]
    seen = defaultdict(list)

    def issue(row, message):
        strict = row.get("schema_version") == 2 or row.get("naming_version") == 2
        result.append({"level": "error" if strict else "warning", "key": row["key"], "message": message})

    for row in catalog["entries"]:
        kind = row["kind"]
        policy = (catalog.get("video_policy") if row.get("medium") == "video" else catalog.get("policy"))
        if policy is None:
            policy = naming.policy_for(taxonomy.vocab)
        if not row["exists"]:
            issue(row, f"broken link: {row['file'] or '(missing path)'}")
        if row.get("missing_record"):
            issue(row, "missing take sidecar")
        if not row["category_id"]:
            issue(row, f"unclassified or unknown category: {row.get('category') or '(empty)'}")
        elif row.get("category_role") and row["category_id"] not in taxonomy.categories(row["category_role"]):
            issue(row, f"category does not allow {row['category_role']}")
        if kind == "card":
            seen[("card ID", naming.name_key(row["card_id"]))].append(row)
            if row.get("schema_version") != 2:
                issue(row, "legacy card naming")
            try:
                naming.validate_card_id(row["card_id"], policy, stem=Path(row["name"]).stem)
                if row.get("pack"):
                    taxonomy.validate_pack(row["pack"])
            except (naming.NamingError, TaxonomyError) as error:
                issue(row, str(error))
        elif kind in {"candidate", "take", "library"}:
            if kind != "library" or row.get("sample_name"):
                seen[(kind + " filename", naming.name_key(row["name"]))].append(row)
            if row.get("card_exists") is False:
                issue(row, f"missing owning card: {row.get('card_id') or '(unknown)'}")
            if kind != "library" or row.get("sample_name"):
                try:
                    parsed = naming.parse_media_name(row.get("sample_name") or row["name"], taxonomy.vocab,
                                                     medium=row.get("medium") or None, policy=policy)
                    if row.get("card_id") and naming.name_key(parsed["card_id"]) != naming.name_key(row["card_id"]):
                        raise naming.NamingError("filename and record disagree on card ID")
                    if not row.get("legacy_card"):
                        naming.validate_card_id(parsed["card_id"], policy)
                    if row.get("naming_version") == 2:
                        if parsed["model_code"] != row.get("model"):
                            raise naming.NamingError("filename and record disagree on canonical model")
                        if row.get("seq") is not None and row["seq"] != parsed["seq"]:
                            raise naming.NamingError("filename and record disagree on sequence")
                    if kind != "library" and not row.get("derived_from"):
                        seen[(kind + " sequence", naming.name_key(parsed["card_id"]),
                              parsed["model_code"], parsed["seq"])].append(row)
                except naming.NamingError as error:
                    issue(row, str(error))
            if row.get("model") and row.get("medium"):
                try:
                    naming.resolve_model(row["model"], taxonomy.vocab, row["medium"])
                except naming.NamingError as error:
                    issue(row, str(error))
    for (label, *identity), rows in seen.items():
        if len(rows) > 1:
            for row in rows:
                issue(row, f"duplicate {label}: {' / '.join(map(str, identity))}")
    return result


def search_catalog(catalog, query, vocab, semantic_fn=None):
    query = query.strip()
    semantic_query = query
    query = naming.name_key(query)
    if not query:
        return []
    entries = {row["key"]: row for row in catalog["entries"]}
    model_map = naming.model_aliases(vocab)
    groups = [[naming.name_key(v) for v in [token, *aliases]]
              for token, aliases in (vocab or {}).get("tags", {}).items()]
    def alternatives(term):
        out = {term}
        if term in model_map:
            out.add(model_map[term])
        for group in groups:
            if term in group:
                out.update(group)
        return out

    keyword, exact = {}, {}
    for key, row in entries.items():
        filename, card, model = (naming.name_key(row.get(field) or "") for field in ("name", "card_id", "model"))
        identity = 3 if query == filename else 2 if query == card else 1 if model and model in alternatives(query) else 0
        text = naming.name_key(" ".join(map(str, [
            filename, card, model, row.get("title", ""), row.get("file", ""), row.get("category", ""),
            row.get("category_id", ""), row.get("category_label", ""), row.get("pack", ""), row.get("tags", [])])))
        score = sum(any(word in text for word in alternatives(term)) for term in query.split())
        score += 2 * any(word in text for word in alternatives(query))
        if score or identity:
            keyword[key] = score
            exact[key] = identity
    keys = sorted(keyword, key=lambda key: (-exact[key], -keyword[key], key))
    rank = {key: 1 / (60 + i) for i, key in enumerate(keys, 1)}
    if semantic_fn is not None:
        try:
            scores = semantic_fn(semantic_query) or {}
            semantic = sorted(((key, score) for key, score in scores.items()
                               if key in entries and isinstance(score, (int, float)) and math.isfinite(score)),
                              key=lambda item: (-item[1], item[0]))
        except Exception:
            semantic = []
        threshold, remaining = (0.62, 3) if keys else (0.54, 5)
        for i, (key, score) in enumerate(semantic, 1):
            if key not in keyword:
                if score < threshold or remaining == 0:
                    continue
                remaining -= 1
            rank[key] = rank.get(key, 0) + 1 / (60 + i)
    return [entries[key] for key in sorted(rank, key=lambda key: (-exact.get(key, 0), -rank[key], key))]


def add_commands(add):
    add("index", cmd_index, "write the project catalog")
    add("check", cmd_check, "check the catalog without writing files")
    parser = add("find", cmd_find, "search the project catalog")
    parser.add_argument("terms", nargs="+")


def cmd_index(cfg, args):
    catalog = build_catalog(cfg, cfg.taxonomy)
    options = cfg.raw.get("catalog") or {}
    md = _path(cfg.root, options.get("index_md", "_INDEX.md"))
    data = _path(cfg.root, options.get("index_json", ".catalog-index.json"))
    for path, content in ((md, render_markdown(catalog)),
                          (data, json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n")):
        atomic_write(path, content)
        print(path)


def cmd_check(cfg, args):
    catalog = build_catalog(cfg, cfg.taxonomy)
    result = check_catalog(catalog, cfg.taxonomy)
    for item in result:
        print(f"{item['level']}: {item['key']}: {item['message']}")
    errors = sum(item["level"] == "error" for item in result)
    print(f"{errors} error(s), {len(result) - errors} warning(s)")
    if errors:
        raise SystemExit(2)


def cmd_find(cfg, args):
    catalog = build_catalog(cfg, cfg.taxonomy)
    for row in search_catalog(catalog, " ".join(args.terms), cfg.taxonomy.vocab):
        print("\t".join(str(part or "") for part in (row["file"], row["kind"], row.get("model"), row.get("title"))))


def migration_count(cfg):
    if not cfg.taxonomy.enabled:
        return 0
    return build_catalog(cfg, cfg.taxonomy)["migrations"]["count"]
