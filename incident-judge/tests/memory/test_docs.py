from __future__ import annotations

from judge.memory.docs import ARCHITECTURE, docs_for, section, service_doc_path
from judge.memory.index import parse_index, render_index
from judge.memory.repo import MemoryRepo


def test_template_ships_service_docs(repo: MemoryRepo):
    paths = repo.list_files("wiki/services")
    assert {"wiki/services/checkout.md", "wiki/services/search.md", "wiki/services/catalog.md",
            "wiki/services/internal-batch.md"} <= set(paths)
    assert repo.read(ARCHITECTURE)
    checkout = repo.read("wiki/services/checkout.md")
    failure_modes = section(checkout, "Failure modes")
    for needle in ("payment_v2", "PoolTimeout", "db_query_p95", "toggle_flag", "scale_pool", "Escalate"):
        assert needle in failure_modes


def test_docs_for_includes_architecture_services_and_related(repo: MemoryRepo, config):
    docs = docs_for(repo, ["checkout"], config)
    paths = [p for p, _ in docs]
    assert paths[0] == ARCHITECTURE and paths[1] == "wiki/services/checkout.md"
    assert "wiki/services/search.md" in paths  # shares the db dependency
    only = [p for p, _ in docs_for(repo, ["checkout@staging"], config, include_related=False)]
    assert only == [ARCHITECTURE, "wiki/services/checkout.md"]
    assert service_doc_path("checkout@staging") == "wiki/services/checkout.md"


def test_docs_for_skips_missing_pages(repo: MemoryRepo):
    assert [p for p, _ in docs_for(repo, ["no-such-service"])] == [ARCHITECTURE]


def test_index_regeneration_keeps_service_docs_section(seeded: MemoryRepo):
    index = seeded.read("wiki/index.md")
    assert "## Service docs" in index and "services/checkout.md" in index
    assert {e["id"] for e in parse_index(index)} == {"checkout-payment-v2-flag", "db-pool-starved"}
    assert "## Service docs" in render_index([])
