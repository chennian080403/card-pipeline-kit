# card-core copy -- do not edit here: edit card-core/core (next to this skill), then run card-core/sync.py
"""Shared v2 filename contract; callers allocate sequences under their manifest lock."""

import re
import unicodedata
from pathlib import Path


class NamingError(ValueError):
    pass


DEFAULT_MAX_BYTES = 120
BAD = re.compile(r'[\s/\\:*?"<>|#%\x00-\x1f\x7f]')
CODE = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")
MEDIA = re.compile(r"(?P<card_id>.+?)__(?P<model_code>[a-z0-9][a-z0-9.-]*?)__(?P<seq>[0-9]{3})(?:_(?P<feature>(?:(?!__)[^.])+))?")


def normalize_name(text):
    return unicodedata.normalize("NFC", str(text))


def name_key(text):
    return normalize_name(normalize_name(text).casefold())


def policy_for(vocab=None, project=None):
    return {**((vocab or {}).get("naming") or {}), **((project or {}).get("naming") or {})}


def validate_name(text, policy=None):
    if not isinstance(text, str) or not text or text in {".", ".."} or BAD.search(text) or text.endswith("."):
        raise NamingError(f"invalid name: {text!r}")
    limit = (policy or {}).get("max_utf8_bytes", DEFAULT_MAX_BYTES)
    if type(limit) is not int or limit < 1:
        raise NamingError("naming.max_utf8_bytes must be a positive integer")
    if text != normalize_name(text):
        raise NamingError(f"name must be Unicode NFC: {text!r}")
    if len(text.encode("utf-8")) > limit:
        raise NamingError(f"name exceeds {limit} UTF-8 bytes: {text!r}")
    return text


def validate_card_id(card_id, policy=None, *, stem=None):
    validate_name(card_id, policy)
    if "_" in card_id:
        raise NamingError(f"card ID must not contain '_': {card_id!r}")
    if stem is not None and card_id != normalize_name(stem):
        raise NamingError(f"card ID {card_id!r} must equal file stem {stem!r}")
    return card_id


def check_card_unique(paths, card_id, *, exclude=None):
    for path in paths:
        if path != exclude and name_key(path.stem) == name_key(card_id):
            raise NamingError(f"card ID collides with {path.name}: {card_id!r}")


def model_aliases(vocab=None, medium=None):
    models = (vocab or {}).get("models") or {}
    if medium is None:
        tables = list(models.values())
    else:
        tables = [models.get(medium) or {}]
    aliases = {}
    ambiguous = set()
    for table in tables:
        for code, entry in table.items():
            if not CODE.fullmatch(code):
                raise NamingError(f"invalid model code: {code!r}")
            if not isinstance(entry, dict):
                raise NamingError(f"model {code}: expected a mapping")
            values = entry.get("aliases", [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise NamingError(f"model {code}: aliases must be a list of names")
            for value in [code, *values]:
                key = name_key(value.strip())
                if key in aliases and aliases[key] != code:
                    if medium is not None:
                        raise NamingError(f"ambiguous model alias: {value!r}")
                    ambiguous.add(key)
                aliases[key] = code
    return {key: value for key, value in aliases.items() if key not in ambiguous}


def has_model_vocab(vocab, medium):
    return bool(((vocab or {}).get("models") or {}).get(medium))


def resolve_model(raw_model, vocab=None, medium=None):
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise NamingError("model is required")
    table = model_aliases(vocab, medium)
    if not table:
        return normalize_name(raw_model.strip())
    code = table.get(name_key(raw_model.strip()))
    if code is None:
        raise NamingError(f"model {raw_model!r} is not in the model vocabulary")
    return code


def _media_card(card_id, policy, legacy_card):
    if not legacy_card:
        return validate_card_id(card_id, policy)
    return validate_name(card_id, policy)


def build_media_name(card_id, model_code, seq, feature="", ext=".png", *, policy=None, legacy_card=False):
    card_id = _media_card(normalize_name(card_id), policy, legacy_card)
    model_code = name_key(model_code)
    if not CODE.fullmatch(model_code):
        raise NamingError(f"invalid model short code: {model_code!r}")
    if type(seq) is not int or not 1 <= seq <= 999:
        raise NamingError("media sequence must be between 001 and 999")
    if not re.fullmatch(r"\.[a-zA-Z0-9]+", ext):
        raise NamingError(f"invalid media extension: {ext!r}")
    feature = normalize_name(feature) if feature else ""
    if feature:
        validate_name(feature, policy)
        if "." in feature or "__" in feature:
            raise NamingError("short feature must not contain '.' or '__'")
    return validate_name(f"{card_id}__{model_code}__{seq:03d}" + (f"_{feature}" if feature else "") + ext.lower(), policy)


def _parse(filename):
    stem, ext = Path(filename).stem, Path(filename).suffix
    match = MEDIA.fullmatch(stem)
    if not match or not re.fullmatch(r"\.[a-zA-Z0-9]+", ext):
        raise NamingError(f"invalid v2 media name: {filename!r}")
    fields = match.groupdict()
    fields.update(seq=int(fields["seq"]), ext=ext, feature=fields["feature"] or "")
    return fields


def parse_media_name(filename, vocab=None, *, medium=None, policy=None):
    policy = policy_for(vocab) if policy is None else policy
    validate_name(filename, policy)
    fields = _parse(filename)
    if resolve_model(fields["model_code"], vocab, medium) != fields["model_code"]:
        raise NamingError(f"model short code is not canonical: {fields['model_code']!r}")
    if build_media_name(**fields, policy=policy, legacy_card=True) != filename:
        raise NamingError(f"noncanonical media name: {filename!r}")
    return fields


def next_seq(existing_names_or_jobs, card_id, model_code):
    highest = 0
    card_key, model_key = name_key(card_id), name_key(model_code)
    def matches(card, model):
        return name_key(card) == card_key and name_key(model) == model_key

    def read_name(value):
        try:
            return _parse(name_key(Path(str(value)).name))
        except NamingError:
            return None

    items = (existing_names_or_jobs.items() if isinstance(existing_names_or_jobs, dict)
             else ((item, None) for item in existing_names_or_jobs))
    for key, job in items:
        if isinstance(key, dict):
            job, key = key, ""
        if isinstance(job, dict):
            key_parts = str(key).split("|")
            key_card, key_model, key_seq = key_parts if len(key_parts) == 3 else ("", "", "")
            if matches(job.get("card_id") or job.get("card") or key_card,
                       job.get("canonical_model") or job.get("model_alias") or key_model):
                seq = str(job.get("seq") or key_seq)
                if seq.isdecimal():
                    highest = max(highest, int(seq))
            values = [job.get(field) for field in ("file", "candidate_name", "take", "name", "media_name")]
        else:
            values = [key]
            parts = str(key).split("|")
            if len(parts) == 3 and matches(*parts[:2]) and parts[2].isdecimal():
                highest = max(highest, int(parts[2]))
        for value in filter(None, values):
            fields = read_name(value)
            if fields and matches(fields["card_id"], fields["model_code"]):
                highest = max(highest, fields["seq"])
    if highest >= 999:
        raise NamingError(f"media sequence exhausted for {card_id} / {model_code}")
    return highest + 1
