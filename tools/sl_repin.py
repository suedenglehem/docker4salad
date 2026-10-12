#!/usr/bin/env python3
"""sl_repin.py — move the deployers' IMAGE pins to new digests, in one pass.

Every image cycle ends the same way: the new images are on Docker Hub, and the
five deployers must point at the new DIGESTS (not tags — the Salad worker image
cache is keyed by repo name and a tag never re-pulls, so a pinned
repo@sha256:… ref is the only airtight lever).

Each deployer keeps a comment block above IMAGE with the human-readable tag form
and a history of previous tag→digest lines. This tool updates all three in step:
the pinned digest, the tag form, and a new history line for the pin it replaces.

  python3 -I tools/sl_repin.py \\
      boris271142/lmss:cuda128-v13=sha256:aaaa… \\
      boris271142/lmss_generic:cuda128-v13=sha256:bbbb… \\
      boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15=sha256:cccc… \\
      boris271142/lmss_generic_ampere:cuda130-v11=sha256:dddd… \\
      --dry-run

Digests come from the Hub (tools/hub_describe.py / the push script prints them),
NOT from `docker images` — the local image id is a config hash, not the manifest
digest. `--dry-run` prints the diff and writes nothing (default: write in place).

Refuses to touch a file whose current digest does not appear exactly once, so a
half-edited deployer never gets silently rewritten.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

# repo -> deployers that pin it (the 9B pair shares the base image)
REPO_DEPLOYERS = {
    "boris271142/lmss": ["deploy_qwen38_9b.py", "deploy_qwen9b.py"],
    "boris271142/lmss_generic": ["deploy_generic.py"],
    "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision": ["deploy_qwen38_27b.py"],
    "boris271142/lmss_generic_ampere": ["deploy_ampere.py"],
}

TAG_RE = re.compile(r"cuda\d{3}-v\d+")
HISTORY_RE = re.compile(r"^#\s+cuda\d{3}-v\d+: sha256:[0-9a-f]{64}\s*$")


def parse_spec(text: str) -> tuple[str, str, str]:
    """'repo:tag=sha256:hex' -> (repo, tag, digest)."""
    left, sep, digest = text.partition("=")
    if not sep or not digest.startswith("sha256:"):
        sys.exit(f"bad spec {text!r}: want repo:tag=sha256:<64 hex>")
    repo, colon, tag = left.rpartition(":")
    if not colon or not repo or not TAG_RE.fullmatch(tag):
        sys.exit(f"bad spec {text!r}: want repo:tag=sha256:<64 hex>")
    if repo not in REPO_DEPLOYERS:
        sys.exit(f"unknown repo {repo!r} (known: {', '.join(sorted(REPO_DEPLOYERS))})")
    if len(digest) != 71 or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        sys.exit(f"bad digest in {text!r}: want sha256: + 64 hex chars")
    return repo, tag, digest


def repin(path: Path, repo: str, tag: str, digest: str) -> str:
    """Return the new file text; raise SystemExit if the file doesn't match expectations."""
    text = path.read_text()
    lines = text.split("\n")

    # 1) the pinned digest INSIDE the IMAGE constant (the comment block above also
    #    carries digests — history lines — so read the assignment block, not the file)
    at = [i for i, ln in enumerate(lines) if ln.startswith("IMAGE = ")]
    if len(at) != 1:
        sys.exit(f"{path.name}: expected exactly one 'IMAGE = ' line, found {len(at)}")
    i = at[0]
    block = "\n".join(lines[i:i + 3])
    if repo not in block:
        sys.exit(f"{path.name}: IMAGE block does not pin {repo}")
    m = re.search(r'sha256:[0-9a-f]{64}', block)
    if not m:
        sys.exit(f"{path.name}: IMAGE block has no sha256: digest pin")
    old_digest = m.group(0)
    if old_digest == digest:
        print(f"{path.name}: already pinned to {digest[:20]}… — skipped")
        return text
    block = block.replace(old_digest, digest)
    lines = lines[:i] + block.split("\n") + lines[i + 3:]

    # 2) the human-readable tag form comment, and 3) the history line for the pin
    #    being retired. The label is the line containing 'tag form' (every deployer
    #    has exactly one); the ref sits on that line or the next. Searching the whole
    #    file would hit the docstring's historical 'FROM …:cuda128-v3' mentions.
    label = [i for i, ln in enumerate(lines) if "tag form" in ln.lower()]
    if len(label) != 1:
        sys.exit(f"{path.name}: expected exactly one 'tag form' comment, found {len(label)}")
    pat = re.compile(rf"{re.escape(repo)}:({TAG_RE.pattern})")
    ref = next((i for i in (label[0], label[0] + 1) if i < len(lines) and pat.search(lines[i])),
               None)
    if ref is None:
        sys.exit(f"{path.name}: no '{repo}:<tag>' ref on/after the tag-form comment")
    old_tag = pat.search(lines[ref]).group(1)
    m = pat.search(lines[ref])
    lines[ref] = lines[ref][:m.start()] + f"{repo}:{tag}" + lines[ref][m.end():]

    # History entry: right after the ref, stepping over the prose that introduces the
    # list in the q6 deployer so the new line lands inside the list, not above it.
    ins = ref + 1
    while (ins < len(lines) and ins - ref <= 2 and lines[ins].lstrip().startswith("#")
           and not HISTORY_RE.match(lines[ins])):
        ins += 1
    lines.insert(ins, f"#   {old_tag}: {old_digest}")
    new = "\n".join(lines)

    # post-conditions: new digest exactly once, old digest gone
    if new.count(digest) != 1:
        sys.exit(f"{path.name}: post-check failed — new digest appears {new.count(digest)}x")
    if old_digest in new.replace(f"#   {old_tag}: {old_digest}", ""):
        sys.exit(f"{path.name}: post-check failed — old digest still in the IMAGE block")
    print(f"{path.name}: {old_tag} -> {tag}  ({old_digest[7:15]}… -> {digest[7:15]}…)")
    return new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("specs", nargs="+", metavar="repo:tag=sha256:…",
                    help="one per image; a repo may cover two deployers (the 9B pair)")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    args = ap.parse_args()

    diffs = []
    for spec in args.specs:
        repo, tag, digest = parse_spec(spec)
        for name in REPO_DEPLOYERS[repo]:
            path = DEPLOY / name
            if not path.is_file():
                sys.exit(f"missing {path}")
            old_text = path.read_text()
            new_text = repin(path, repo, tag, digest)
            diff = "".join(difflib.unified_diff(
                old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}"))
            diffs.append((path, new_text, diff))

    for path, new_text, diff in diffs:
        print(diff if diff else f"(no change) {path.name}")
        if not args.dry_run:
            path.write_text(new_text)
    print(("DRY RUN — nothing written" if args.dry_run
           else f"WROTE {len(diffs)} file(s): " + ", ".join(p.name for p, _, _ in diffs))
          + ". Next: python3 -I tools/test_deploy_offline.py, then commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
