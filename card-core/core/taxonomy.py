"""Project vocabulary and the adapter for older project configurations."""

from pathlib import Path
import re

import yaml

from . import naming

ROLES = ("reference", "source", "deliverable", "evaluation", "text")
DEFAULT_VOCAB_PATH = Path(__file__).with_name("vocab.example.yaml")


class TaxonomyError(ValueError):
    pass


def _mapping(value, field):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TaxonomyError(f"{field} must be a mapping with string keys")
    return value


def _name(value, field):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or value in {".", ".."} or any(c in value for c in "/\\\x00")
            or any(ord(c) < 32 for c in value)):
        raise TaxonomyError(f"invalid {field}: {value!r}")
    return value


def _names(value, field):
    if not isinstance(value, list):
        raise TaxonomyError(f"{field} must be a list")
    return [_name(item, field) for item in value]


class Taxonomy:
    def __init__(self, vocab, *, enabled=True, fallback_kinds=None):
        self.vocab = _mapping(vocab, "vocab")
        if self.vocab.get("version", 1) != 1:
            raise TaxonomyError("unsupported vocab version")
        self.enabled = enabled
        self._fallback_kinds = fallback_kinds
        self.locale = _name(vocab.get("locale", "zh"), "locale")
        self.unclassified = _name(vocab.get("default_unclassified", "未归类"), "default_unclassified")
        self._categories = {}
        self._tiers = [{}, {}, {}, {}]
        self._migrations = {}
        self._default_reference = None

        def add(tier, token, cid):
            key = naming.name_key(token)
            prior = self._tiers[tier].get(key)
            if prior is not None and prior != cid:
                raise TaxonomyError(f"ambiguous category token: {token!r}")
            self._tiers[tier][key] = cid

        for cid, spec in _mapping(vocab.get("categories", {}), "categories").items():
            if enabled and not re.fullmatch(r"[a-z][a-z0-9_-]*", cid):
                raise TaxonomyError(f"invalid category ID: {cid!r}")
            _mapping(spec, f"categories.{cid}")
            labels = _mapping(spec.get("labels", {}), f"{cid}.labels")
            for value in labels.values():
                _name(value, f"{cid}.labels")
            label = _name(spec.get("label", labels.get(self.locale, cid)), f"{cid}.label")
            storage = _name(spec.get("storage", label), f"{cid}.storage")
            aliases = _names(spec.get("aliases", []), f"{cid}.aliases")
            flags = _mapping(spec.get("behavior", {}), f"{cid}.behavior")
            if any(key not in ROLES or type(value) is not bool for key, value in flags.items()):
                raise TaxonomyError(f"{cid}.behavior must contain known roles with boolean values")
            meta = _mapping(spec.get("source_meta", {}), f"{cid}.source_meta")
            if set(meta) - {"section", "as", "role"}:
                raise TaxonomyError(f"unknown {cid}.source_meta field")
            if flags.get("source"):
                meta = {"section": cid, "as": cid, "role": "the {name}", **meta}
                for key in ("section", "as"):
                    _name(meta[key], f"{cid}.source_meta.{key}")
                    if ":" in meta[key] or any(c.isspace() for c in meta[key]):
                        raise TaxonomyError(f"invalid source {key}: {meta[key]!r}")
                if not isinstance(meta["role"], str) or not meta["role"].strip():
                    raise TaxonomyError(f"{cid}.source_meta.role must be text")
            self._categories[cid] = {"label": labels.get(self.locale, label), "labels": dict(labels),
                                     "aliases": aliases, "storage": storage,
                                     "behavior": {role: flags.get(role, False) for role in ROLES},
                                     "source_meta": dict(meta)}
            add(0, cid, cid)
            for token in [label, *labels.values()]:
                add(1, token, cid)
            for token in [storage, *aliases]:
                add(2, token, cid)
        if enabled and not self._categories:
            raise TaxonomyError("vocab.categories must contain at least one category")
        migrations = _mapping(vocab.get("migrations", {}), "migrations")
        pending = {}
        for kind in ("aliases", "merge"):
            for token, target in _mapping(migrations.get(kind, {}), f"migrations.{kind}").items():
                key = naming.name_key(_name(token, f"migrations.{kind} key"))
                target = naming.name_key(_name(target, f"migrations.{kind} target"))
                if key in pending and pending[key] != target:
                    raise TaxonomyError(f"conflicting migration for {token!r}")
                pending[key] = target

        def destination(key, trail):
            for tier in self._tiers[:3]:
                if key in tier:
                    return tier[key]
            if key in trail or key not in pending:
                raise TaxonomyError(f"unresolved or cyclic category migration: {key!r}")
            return destination(pending[key], trail | {key})

        for key, target in pending.items():
            self._migrations[key] = destination(target, {key})
            add(3, key, self._migrations[key])
        ref = vocab.get("default_reference")
        if ref is not None:
            self._default_reference = self.resolve(_name(ref, "default_reference"))
            if self._default_reference not in self.categories("reference"):
                raise TaxonomyError("default_reference must name a reference category")
        elif "style" in self.categories("reference"):
            self._default_reference = "style"
        else:
            self._default_reference = next(iter(self.categories("reference")), None)
        if enabled:
            self.to_sources_kinds()
            models = _mapping(vocab.get("models", {}), "models")
            for medium, table in models.items():
                _mapping(table, f"models.{medium}")
                naming.model_aliases(vocab, medium)
            for token, values in _mapping(vocab.get("tags", {}), "tags").items():
                _name(token, "tag")
                _names(values, f"tags.{token}")
            policy = _mapping(vocab.get("naming", {}), "naming")
            naming.validate_name("x", policy)
            if "strict_v2" in policy and type(policy["strict_v2"]) is not bool:
                raise TaxonomyError("naming.strict_v2 must be boolean")

    def resolve(self, token):
        key = naming.name_key(str(token or "").strip())
        return next((tier[key] for tier in self._tiers if key in tier), None)

    def categories(self, role=None):
        if role is not None and role not in ROLES:
            raise TaxonomyError(f"unknown category role: {role!r}")
        return {cid: spec for cid, spec in self._categories.items()
                if role is None or spec["behavior"][role]}

    def label(self, cid):
        return self._categories[cid]["label"] if cid in self._categories else self.unclassified

    def storage(self, cid):
        return self._categories[cid]["storage"] if cid in self._categories else self.unclassified

    def validate_pack(self, parts, role=None):
        if isinstance(parts, str):
            parts = parts.replace("\\", "/").split("/")
        parts = list(parts or [])
        if not parts:
            raise TaxonomyError("pack needs a top-level category")
        for part in parts:
            _name(part, "pack component")
        cid = self.resolve(parts[0])
        if cid is None:
            raise TaxonomyError(f"unknown pack category: {parts[0]!r}")
        if role and cid not in self.categories(role):
            raise TaxonomyError(f"category {parts[0]!r} does not allow {role}")
        return cid

    def default_reference_category(self):
        return self._default_reference

    def to_sources_kinds(self):
        if self._fallback_kinds is not None:
            return {key: dict(value) for key, value in self._fallback_kinds.items()}
        result, tokens = {}, {}
        for cid, spec in self.categories("source").items():
            meta = spec["source_meta"]
            section = meta["section"]
            if section in result:
                raise TaxonomyError(f"duplicate source section: {section!r}")
            for token in (section, meta["as"]):
                key = naming.name_key(token)
                if key in tokens and tokens[key] != cid:
                    raise TaxonomyError(f"ambiguous source token: {token!r}")
                tokens[key] = cid
            result[section] = {"label": self.label(cid), "as": meta["as"],
                               "role": meta["role"], "text": spec["behavior"]["text"]}
        return result

    def migration_report(self, tokens):
        counts = {}
        for token in tokens:
            key = naming.name_key(token)
            cid = self.resolve(token)
            if cid is not None and self._migrations.get(key) == cid:
                counts[token] = counts.get(token, 0) + 1
        return {"count": sum(counts.values()),
                "aliases": {key: {"category_id": self.resolve(key), "count": count}
                            for key, count in sorted(counts.items())}}


def load(project_raw, project_root):
    """Load taxonomy relative to the config directory, or adapt legacy pack settings."""
    raw = project_raw or {}
    if "taxonomy" in raw:
        ref = raw["taxonomy"]
        if not isinstance(ref, str) or not ref.strip():
            raise TaxonomyError("taxonomy must point to a vocab.yaml file")
        path = Path(project_root) / Path(ref).expanduser()
        try:
            return Taxonomy(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, yaml.YAMLError, TaxonomyError, naming.NamingError) as error:
            raise TaxonomyError(f"taxonomy {path}: {error}") from error
    from . import sources

    # Legacy settings keep their original spelling and separate reference allowlist.
    kinds = sources.kinds(raw)
    packs = raw.get("packs") or {}
    reference = ((raw.get("inspiration") or {}).get("categories")
                 or packs.get("inspiration_categories") or ["人物", "道具", "场景", "风格"])
    labels = [str(x) for x in (packs.get("categories") or ["人物", "道具", "场景", "风格", "测试"])]
    by_label = {str(spec["label"]): section for section, spec in kinds.items()}
    categories = {}
    for label in dict.fromkeys([*labels, *map(str, reference), *by_label, "评测"]):
        categories[label] = {"behavior": {"reference": label in reference, "source": label in by_label,
                                         "evaluation": label == "评测"}}
    default = "风格" if "风格" in reference else str(reference[0])
    try:
        return Taxonomy({"categories": categories, "default_reference": default},
                        enabled=False, fallback_kinds=kinds)
    except TaxonomyError:
        # Unusual legacy labels remain usable by the old write path.
        return Taxonomy({"categories": {}}, enabled=False, fallback_kinds=kinds)


def migration_report(taxonomy, tokens):
    return taxonomy.migration_report(tokens)
