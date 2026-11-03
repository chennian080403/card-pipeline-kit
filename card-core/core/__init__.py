"""card-core: the shared base of the card pipelines (image / video).

One source in card-core/ (next to the skills); each skill carries a copy in scripts/core/
so it still works on its own. Edit here, then `python3 card-core/sync.py`;
`sync.py --check` fails when a copy drifted. See card-core/README.md.
"""

VERSION = "1.0.0"
