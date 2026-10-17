"""Tests for humanizer/scripts/scan.py and humanizer/hooks/humanizer_nudge.py.

Each test builds a throwaway repo, runs the script as a subprocess, and checks
the JSON it prints. Commits pass their identity through `git -c`.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "humanizer" / "scripts" / "scan.py"
HOOK = ROOT / "humanizer" / "hooks" / "humanizer_nudge.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "humanizer"

spec = importlib.util.spec_from_file_location("humanizer_scan", str(SCAN))
scan_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_module)

FILLER = ("The parser reads the config file once at startup and keeps it in memory. "
          "Each command checks its own flags before it touches the disk. ") * 12


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def env():
    e = dict(os.environ)
    for k in ("HUMANIZER_DISABLE", "HUMANIZER_SCAN_TIMEOUT"):
        e.pop(k, None)
    return e


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")

    class Repo:
        root = path

        def write(self, name, text):
            f = path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                f.write_bytes(text)
            else:
                f.write_text(text, encoding="utf-8")
            return f

        def commit(self):
            git(path, "add", "-A")
            git(path, "commit", "-qm", "chore: base")

        def config(self, cfg):
            self.write(".claude/humanizer.json", cfg if isinstance(cfg, str) else json.dumps(cfg))

        def scan(self, *args, cwd=None):
            r = subprocess.run([sys.executable, str(SCAN), *args], cwd=str(cwd or path),
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr
            return json.loads(r.stdout)

    return Repo()


def ids(result):
    return [f["id"] for r in result["regions"] for f in r["findings"]]


def findings(result, fid=None):
    return [dict(f, file=r["file"]) for r in result["regions"] for f in r["findings"]
            if fid is None or f["id"] == fid]


# ---------------------------------------------------------------- scope


def test_only_added_lines_are_scanned(repo):
    repo.write("app.py", "x = 1\n# We delve into the old code here\ny = 2\n")
    repo.commit()
    repo.write("app.py", "x = 1\n# We delve into the old code here\ny = 2\n"
                         "# A seamless addition\nz = 3\n")
    result = repo.scan()
    assert result["mode"] == "working-tree"
    assert [(f["id"], f["line"]) for f in findings(result)] == [("phrase.seamless", 4)]


def test_unchanged_old_text_is_ignored(repo):
    repo.write("README.md", "# Title\n\nIt is important to note this is a game-changer.\n\n"
                            "Install it.\n")
    repo.commit()
    repo.write("README.md", "# Title\n\nIt is important to note this is a game-changer.\n\n"
                            "Install it with pip.\n")
    result = repo.scan()
    assert result["regions"] == []
    assert result["summary"]["regions_examined"] == 1


def test_staged_reads_the_index_not_the_working_tree(repo):
    repo.write("a.py", "# a seamless comment\nx = 1\n")
    git(repo.root, "add", "a.py")
    repo.write("a.py", "# plain comment about retries\nx = 1\n")
    assert ids(repo.scan("--staged")) == ["phrase.seamless"]


def test_diff_mode_scans_changes_since_merge_base(repo):
    repo.write("a.py", "x = 1\n")
    repo.commit()
    git(repo.root, "branch", "base")
    repo.write("a.py", "x = 1\n# we leverage the cache\n")
    repo.commit()
    result = repo.scan("--diff", "base")
    assert result["mode"] == "diff"
    assert ids(result) == ["phrase.leverage"]


def test_untracked_files_are_scanned_whole(repo):
    repo.write("seed.txt", "seed\n")
    repo.commit()
    repo.write("new.md", "This is a testament to the design.\n")
    assert ids(repo.scan()) == ["phrase.testament"]


def test_explicit_paths_scan_whole_files(repo):
    repo.write("old.py", "# a robust helper\nx = 1\n")
    repo.commit()
    assert ids(repo.scan("old.py")) == ["phrase.robust"]


def test_gitignored_paths_are_skipped(repo):
    repo.write(".gitignore", "build/\n")
    repo.write("build/out.py", "# delve\n")
    result = repo.scan("build/out.py")
    assert result["skipped"] == [{"file": "build/out.py", "reason": "gitignored"}]
    assert result["regions"] == []


# ---------------------------------------------------------------- extractors

LANG_CASES = {
    "py": ("app.py", 's = "# delve"\n# we delve here\nx = 1\n', 2),
    "py-docstring": ("app.py", 'def f():\n    """We delve here."""\n    return 1\n', 2),
    "js": ("app.js", 'const s = "// delve";\n// we delve here\nrun();\n', 2),
    "ts-block": ("app.ts", "const s = '/* delve */';\n/* we\n * delve here */\nrun();\n", 3),
    "c": ("app.c", 'char *s = "/* delve */";\nint x; // we delve here\n', 2),
    "rust": ("lib.rs", "fn f<'a>(s: &'a str) {}\n// we delve here\n", 2),
    "shell": ("run.sh", 'echo "# delve" $#\nls # we delve here\n', 2),
    "yaml": ("ci.yml", "url: http://x/a#delve\n# we delve here\n", 2),
    "sql": ("q.sql", "SELECT '-- delve';\n-- we delve here\n", 2),
    "html": ("page.html", '<p title="delve">x</p>\n<!-- we delve here -->\n', 2),
    "md": ("notes.md", "# Notes\n\nWe delve here.\n", 3),
    "rst": ("notes.rst", "Title\n=====\n\nWe delve here::\n\n    delve in a literal block\n", 4),
}


@pytest.mark.parametrize("case", sorted(LANG_CASES))
def test_comment_extractors(repo, case):
    name, text, line = LANG_CASES[case]
    repo.write(name, text)
    found = findings(repo.scan(name), "phrase.delve")
    assert [f["line"] for f in found] == [line]


def test_markdown_skips_fenced_and_inline_code_and_urls(repo):
    repo.write("doc.md", "# Doc\n\nRun `delve --robust` from https://example.com/delve.\n\n"
                         "```python\n# it is important to note\n```\n\n~~~\ndelve\n~~~\n")
    assert repo.scan("doc.md")["regions"] == []


def test_license_header_is_skipped(repo):
    repo.write("lib.py", "# Copyright 2026 Example Corp. Licensed under the MIT license.\n"
                         "# It is important to note the robust terms.\nx = 1\n")
    repo.write("lib.js", "// SPDX-License-Identifier: MIT -- a seamless license\nrun();\n")
    assert repo.scan("lib.py", "lib.js")["regions"] == []


@pytest.mark.parametrize("name,text,reason", [
    ("gen.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\n// delve\n", "generated"),
    ("blob.py", b"# delve\n\0\1\2", "binary"),
    ("yarn.lock", "# delve\n", "lockfile"),
    ("vendor/lib.js", "// delve\n", "vendored"),
    ("app.min.js", "// delve\n", "minified"),
    ("LICENSE.md", "It is important to note the terms.\n", "license"),
])
def test_files_that_are_never_scanned(repo, name, text, reason):
    repo.write(name, text)
    result = repo.scan(name)
    assert result["skipped"] == [{"file": name, "reason": reason}]
    assert result["regions"] == []


def test_config_ignore_globs(repo):
    repo.config({"ignore": ["docs/vendor/**", "**/*.generated.*"]})
    repo.write("docs/vendor/x.md", "We delve.\n")
    repo.write("src/api.generated.ts", "// delve\n")
    result = repo.scan()
    assert result["regions"] == []
    assert {s["reason"] for s in result["skipped"]} == {"ignored by config"}


def test_released_changelog_entries_are_skipped(repo):
    repo.write("CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- A seamless fix.\n\n"
                               "## [1.0.0] - 2026-01-01\n\n- A robust release.\n")
    assert ids(repo.scan("CHANGELOG.md")) == ["phrase.seamless"]


# ---------------------------------------------------------------- rules

PHRASE_SAMPLES = {
    "phrase.important-to-note": "It is important to note the cache is cold.",
    "phrase.worth-noting": "It is worth noting that retries are capped.",
    "phrase.delve": "This section delves into the parser.",
    "phrase.dive-into": "This section dives into the config.",
    "phrase.fast-paced": "In today's fast-paced world, builds matter.",
    "phrase.navigate-complexities": "This helps you navigate the complexities of auth.",
    "phrase.tapestry": "A rich tapestry of modules.",
    "phrase.leverage": "The worker can leverage the cache to skip work.",
    "phrase.seamless": "Upgrades are seamless.",
    "phrase.robust": "A robust parser.",
    "phrase.comprehensive-solution": "A comprehensive solution for logs.",
    "phrase.cutting-edge": "Built on cutting-edge tech.",
    "phrase.game-changer": "This flag is a game-changer.",
    "phrase.unlock": "Unlock the full potential of your logs.",
    "phrase.empower": "It empowers teams to ship.",
    "phrase.streamline": "This streamlines the release.",
    "phrase.in-conclusion": "In conclusion, use the flag.",
    "phrase.overall": "Tests pass. Overall, the change is safe.",
    "phrase.this-ensures": "Retries are capped. This ensures progress.",
    "phrase.crucial-role": "The cache plays a crucial role in startup.",
    "phrase.testament": "The uptime is a testament to the design.",
    "formula.not-only": "It not only parses input but also validates it.",
    "formula.whether-you": "Whether you're new or experienced, read on.",
    "hedge.pile-up": "This may potentially fail.",
}


def test_every_phrase_rule_has_a_sample():
    assert {pid for pid, _, _ in scan_module.PHRASES} == set(PHRASE_SAMPLES)


@pytest.mark.parametrize("pid", sorted(PHRASE_SAMPLES))
def test_phrase_rule_fires(repo, pid):
    repo.write("notes.md", PHRASE_SAMPLES[pid] + "\n")
    assert pid in ids(repo.scan("notes.md"))


def test_allow_phrases_suppress_a_rule(repo):
    repo.config({"allowPhrases": ["robust"]})
    repo.write("notes.md", "A robust parser.\n")
    assert repo.scan("notes.md")["regions"] == []


def test_extra_phrases_are_flagged(repo):
    repo.config({"extraPhrases": ["circle back"]})
    repo.write("notes.md", "We will circle back on this.\n")
    assert ids(repo.scan("notes.md")) == ["phrase.extra"]


def test_leverage_as_a_noun_is_not_flagged(repo):
    repo.write("notes.md", "The leverage is low here.\n")
    assert repo.scan("notes.md")["regions"] == []


def test_ensure_that_overuse(repo):
    repo.write("notes.md", "Ensure that the key exists. Then ensure that it is fresh.\n")
    assert "phrase.ensure-that" in ids(repo.scan("notes.md"))


def test_restating_comment_is_flagged(repo):
    repo.write("a.py", "i = 0\n# increment i\ni += 1\n")
    repo.write("b.js", "// loop through the array\nfor (const item of items) {\n  send(item);\n}\n")
    found = findings(repo.scan("a.py", "b.js"), "comment.restates-code")
    assert sorted((f["file"], f["line"]) for f in found) == [("a.py", 2), ("b.js", 1)]


def test_comment_with_a_reason_is_not_flagged(repo):
    repo.write("a.py", "i = 0\n# bump i because the API drops the first request\ni += 1\n")
    assert repo.scan("a.py")["regions"] == []


def test_echo_only_docstring_is_flagged(repo):
    repo.write("a.py", 'def get_user(user_id):\n    """Get user.\n\n    Args:\n'
                       '        user_id: The user id.\n\n    Returns:\n        The user.\n    """\n'
                       "    return db[user_id]\n")
    assert ids(repo.scan("a.py")) == ["docstring.echo"]


def test_informative_docstring_is_not_flagged(repo):
    repo.write("a.py", 'def get_user(user_id):\n    """Read from the replica; the primary is '
                       'rate limited."""\n    return db[user_id]\n')
    assert repo.scan("a.py")["regions"] == []


def test_tutorial_voice_in_comments(repo):
    repo.write("a.js", "// Now we parse the header\nparse(h);\n")
    assert "comment.tutorial-voice" in ids(repo.scan("a.js"))


def test_over_explaining_comment(repo):
    repo.write("a.py", "x = 1  # set the variable to the number one which is used later on\n")
    assert ids(repo.scan("a.py")) == ["comment.over-explains"]
    repo.config({"commentRatio": 20})
    assert repo.scan("a.py")["regions"] == []


def test_hype_and_rule_of_three(repo):
    repo.write("notes.md", "A powerful, innovative and truly seamless tool.\n\n"
                           "It is fast, reliable, and secure.\n")
    found = ids(repo.scan("notes.md"))
    assert "hype-stack" in found and "formula.rule-of-three" in found


def test_uniform_sentences_and_repeated_openers(repo):
    repo.write("notes.md", " ".join(["It reads the file from disk now."] * 5) + "\n")
    found = ids(repo.scan("notes.md"))
    assert "style.uniform-sentences" in found and "style.repeated-openers" in found


@pytest.mark.parametrize("comment", [
    "x = 1  # noqa: E501 delve into it",
    "x = 1  # type: ignore[assignment] a seamless cast",
    "# pylint: disable=robust-check delve",
    "#!/usr/bin/env python  delve",
    "# -*- coding: utf-8 -*- delve",
])
def test_python_lint_directives_are_never_flagged(repo, comment):
    repo.write("a.py", comment + "\n")
    assert repo.scan("a.py")["regions"] == []


@pytest.mark.parametrize("comment", [
    "// eslint-disable-next-line no-console -- a seamless log",
    "// @ts-expect-error delve into types",
    "/* istanbul ignore next: robust */",
    "// prettier-ignore a game-changer",
])
def test_js_lint_directives_are_never_flagged(repo, comment):
    repo.write("a.ts", comment + "\nrun();\n")
    assert repo.scan("a.ts")["regions"] == []


# ---------------------------------------------------------------- voice sample


def test_emoji_prefix_flagged_when_docs_have_none(repo):
    repo.write("README.md", "# Tool\n\n" + FILLER + "\n")
    repo.commit()
    repo.write("NOTES.md", "## ✅ Done\n")
    assert [f["severity"] for f in findings(repo.scan(), "style.emoji-prefix")] == ["medium"]


def test_emoji_prefix_downgraded_when_docs_use_emoji(repo):
    repo.write("README.md", "# 🚀 Tool\n\n" + FILLER + "\n")
    repo.commit()
    repo.write("NOTES.md", "## ✅ Done\n")
    (f,) = findings(repo.scan(), "style.emoji-prefix")
    assert f["severity"] == "info" and f["downgraded"]


EM_DASH = "The cache — when warm — skips the disk — most of the time — and saves a read.\n"


def test_em_dash_threshold(repo):
    repo.write("notes.md", EM_DASH)
    assert "style.em-dash" in ids(repo.scan("notes.md"))
    repo.config({"emDashPer100Words": 50})
    assert "style.em-dash" not in ids(repo.scan("notes.md"))


def test_em_dash_downgraded_when_docs_use_them(repo):
    repo.write("README.md", "# Tool\n\n" + FILLER.replace(" once ", " — once — ") + "\n")
    repo.commit()
    repo.write("notes.md", EM_DASH)
    (f,) = findings(repo.scan(), "style.em-dash")
    assert f["severity"] == "info"


def test_phrase_the_repo_already_uses_is_downgraded(repo):
    repo.write("README.md", "# Tool\n\nA robust parser. A robust writer.\n\n" + FILLER + "\n")
    repo.commit()
    repo.write("notes.md", "Robust retries.\n")
    result = repo.scan()
    (f,) = findings(result, "phrase.robust")
    assert f["severity"] == "info"
    assert result["voice"]["sample"]["words"] >= 200


def test_voice_sample_excludes_the_scanned_lines(repo):
    repo.write("README.md", "# Tool\n\n" + FILLER + "\n")
    repo.commit()
    repo.write("README.md", "# Tool\n\n" + FILLER + "\n\nA robust parser. A robust writer.\n")
    found = findings(repo.scan(), "phrase.robust")
    assert [f["severity"] for f in found] == ["low", "low"]  # its own lines aren't the sample


def test_voice_deviation_reported(repo):
    repo.write("README.md", "# Tool\n\n" + FILLER + "\n")
    repo.commit()
    repo.write("NOTES.md", "## ✅ Done\n")
    assert "uses emoji; the repo's docs don't" in repo.scan()["voice"]["deviations"]


def test_output_carries_the_heuristics_note_and_scores(repo):
    repo.write("notes.md", "A robust parser.\n")
    result = repo.scan("notes.md")
    assert "not a measure of authorship" in result["note"]
    assert result["summary"]["score"] == result["regions"][0]["score"] == 1


# ---------------------------------------------------------------- config and errors


@pytest.mark.parametrize("cfg", ["not json", "[]", {"commentRatio": -1}, {"ignore": "x"},
                                 {"commitNudge": "yes"}])
def test_invalid_config_falls_back_to_defaults(repo, cfg):
    repo.config(cfg)
    repo.write("notes.md", "A robust parser.\n")
    result = repo.scan("notes.md")
    assert result["ok"] and result["config"] == scan_module.DEFAULT_CONFIG
    assert "ignoring invalid" in result["warnings"][0]


def test_unknown_config_key_warns(repo):
    repo.config({"allowPhrases": ["robust"], "colour": "blue"})
    result = repo.scan()
    assert result["config"]["allowPhrases"] == ["robust"]
    assert "unknown key 'colour'" in result["warnings"][0]


@pytest.mark.parametrize("args,code", [
    (["--staged", "--diff", "main"], "bad-arguments"),
    (["--bogus"], "bad-arguments"),
    (["missing.py"], "bad-arguments"),
    (["--diff", "no-such-branch"], "base-not-found"),
])
def test_errors_are_json(repo, args, code):
    result = repo.scan(*args)
    assert result == {"ok": False, "error": {"code": code, "message": result["error"]["message"]}}


def test_not_a_repo_is_json(repo, tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    assert repo.scan(cwd=outside)["error"]["code"] == "not-a-repo"
    (outside / "a.md").write_text("A seamless page.\n")
    assert ids(repo.scan("a.md", cwd=outside)) == ["phrase.seamless"]


def test_empty_repo_is_json(repo):
    result = repo.scan()
    assert result["ok"] and result["regions"] == []


# ---------------------------------------------------------------- fixtures


def fixture_cases():
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", fixture_cases(), ids=lambda c: c["name"])
def test_fixture_corpus(repo, case):
    for stage in ("before", "after"):
        src = FIXTURES / "{}.{}{}".format(case["name"], stage, case["ext"])
        repo.write(src.name, src.read_bytes())
    before = repo.scan("{}.before{}".format(case["name"], case["ext"]))
    after = repo.scan("{}.after{}".format(case["name"], case["ext"]))
    assert set(case["expect"]) <= set(ids(before))
    assert [f for f in findings(after) if f["severity"] in ("medium", "high")] == []

