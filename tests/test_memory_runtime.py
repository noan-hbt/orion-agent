from memory_store import MemoryStore


def test_retrieval_filters_scope_consent_status_and_freshness():
    store = MemoryStore()
    try:
        good = store.put("alpha project decision", namespace="u", scope="project", confidence=.9, freshness=.9)
        store.put("alpha private", namespace="u", scope="project", consent=False)
        store.put("alpha stale", namespace="u", scope="project", freshness=.1)
        store.put("alpha other scope", namespace="u", scope="other")
        store.put("alpha retracted", namespace="u", scope="project", status="retracted")
        result = store.search("alpha", namespace="u", scope="project", min_freshness=.5)
        assert [x.id for x in result] == [good.id]
    finally:
        store.close()


def test_retrieval_is_relevance_ranked_and_bounded():
    store = MemoryStore()
    try:
        store.put("unrelated", namespace="u", confidence=1)
        exact = store.put("the deployment plan is approved", namespace="u", confidence=.5)
        assert store.search("deployment plan", namespace="u", limit=1)[0].id == exact.id
    finally:
        store.close()


def test_assertions_and_forget_query():
    store = MemoryStore()
    try:
        item = store.put("remember this", namespace="u")
        assert store.assert_item(item.id, namespace="u")
        assert store.forget_query("remember", namespace="u") == 1
        assert not store.assert_memory(item.id, namespace="u")
    finally:
        store.close()
