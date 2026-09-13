from __future__ import annotations

import pytest

from judge.core.models import AutonomyLevel
from judge.memory.index import parse_index
from judge.memory.repo import GitError, MemoryRepo
from judge.memory.schema import REQUIRED_SECTIONS, Runbook, RunbookParseError
from judge.settings import ROOT
from tests.memory.conftest import FIXTURES


def test_fixture_roundtrip_is_stable():
    for name in ["checkout-payment-v2-flag.md", "db-pool-starved.md"]:
        rb = Runbook.parse((FIXTURES / name).read_text(encoding="utf-8"))
        assert rb.missing_sections() == []
        again = Runbook.parse(rb.to_markdown())
        assert again.to_markdown() == rb.to_markdown()
        assert again.frontmatter == rb.frontmatter
        assert list(again.sections)[: len(REQUIRED_SECTIONS)] == REQUIRED_SECTIONS


def test_parse_rejects_missing_frontmatter():
    with pytest.raises(RunbookParseError):
        Runbook.parse("# no frontmatter\n")


def test_repo_refuses_project_root_and_non_var_paths():
    with pytest.raises(GitError):
        MemoryRepo(ROOT)
    with pytest.raises(GitError):
        MemoryRepo(ROOT / "judge" / "memory-x")
    MemoryRepo(ROOT / "var" / "memory-guard-test")  # allowed (not created until ensure)


def test_ensure_creates_repo_without_remote(repo):
    assert repo.read("AGENTS.md").startswith("# AGENTS.md")
    assert repo._git("remote").strip() == ""
    assert repo._git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    repo.ensure()  # idempotent


def test_seed_regenerates_index(seeded):
    ids = {e["id"] for e in parse_index(seeded.read("wiki/index.md"))}
    assert ids == {"checkout-payment-v2-flag", "db-pool-starved"}
    rb = seeded.get_runbook("db-pool-starved")
    assert rb.frontmatter.action.name == "scale_pool"


def test_proposal_does_not_touch_main_and_merge_reapplies_code_owned(seeded):
    main_before = seeded.head()
    rb = seeded.get_runbook("db-pool-starved")
    # code sets real numbers on main
    rb.frontmatter.stats.success = 3
    rb.frontmatter.autonomy.level = AutonomyLevel.L1
    seeded.commit_code_owned({rb.path: rb.to_markdown()}, "stats")

    tampered = seeded.get_runbook("db-pool-starved")
    tampered.sections["Notes"] = "New note from the LLM."
    tampered.frontmatter.stats.success = 99
    tampered.frontmatter.autonomy.level = AutonomyLevel.L3
    prop = seeded.create_proposal({tampered.path: tampered.to_markdown()}, "update", "body")
    assert seeded.get_runbook("db-pool-starved").sections["Notes"] != "New note from the LLM."
    assert seeded.head() != main_before  # the stats commit only

    # code keeps moving main after the proposal was opened
    rb2 = seeded.get_runbook("db-pool-starved")
    rb2.frontmatter.stats.success = 4
    seeded.commit_code_owned({rb2.path: rb2.to_markdown()}, "stats again")

    seeded.merge_proposal(prop.id)
    merged = seeded.get_runbook("db-pool-starved")
    assert merged.sections["Notes"] == "New note from the LLM."
    assert merged.frontmatter.stats.success == 4
    assert merged.frontmatter.autonomy.level == AutonomyLevel.L1
    assert seeded.proposal(prop.id).status == "merged"
    with pytest.raises(GitError):
        seeded.merge_proposal(prop.id)


def test_merge_new_page_resets_code_owned_and_unions_log(seeded):
    rb = seeded.get_runbook("db-pool-starved").copy()
    rb.frontmatter.id = "catalog-pooltimeout"
    rb.frontmatter.title = "Catalog pool"
    rb.frontmatter.signatures.fingerprints = ["aaaaaaaaaaaa"]
    rb.frontmatter.stats.success = 50
    rb.frontmatter.autonomy.level = AutonomyLevel.L3
    log_main = seeded.read("wiki/log.md")
    prop = seeded.create_proposal({rb.path: rb.to_markdown(), "wiki/log.md": log_main + "- proposal line\n"},
                                  "new", "")
    seeded.commit_code_owned({"wiki/log.md": log_main + "- code line\n"}, "log")
    seeded.merge_proposal(prop.id)
    new = seeded.get_runbook("catalog-pooltimeout")
    assert new.frontmatter.stats.success == 0 and new.frontmatter.autonomy.level == AutonomyLevel.L0
    log = seeded.read("wiki/log.md")
    assert "- code line" in log and "- proposal line" in log
    assert "catalog-pooltimeout" in {e["id"] for e in parse_index(seeded.read("wiki/index.md"))}


def test_reject_and_moved_branch(seeded):
    rb = seeded.get_runbook("db-pool-starved")
    rb.sections["Notes"] = "x"
    prop = seeded.create_proposal({rb.path: rb.to_markdown()}, "t", "")
    seeded._git("update-ref", f"refs/heads/{prop.branch}", seeded.head())
    with pytest.raises(GitError):
        seeded.merge_proposal(prop.id)
    seeded.reject_proposal(prop.id, "moved")
    assert seeded.proposals("rejected")[0].id == prop.id
