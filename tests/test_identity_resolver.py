import pytest

from identity_resolver import ConversationThread, IdentityResolver, Principal


def test_resolution_is_stable_and_namespaced_by_channel_and_scope():
    resolver = IdentityResolver()
    a = resolver.resolve_principal("telegram", "tenant-a", "42")
    assert a == resolver.resolve_principal("telegram", "tenant-a", 42)
    assert a != resolver.resolve_principal("discord", "tenant-a", "42")
    assert a != resolver.resolve_principal("telegram", "tenant-b", "42")


def test_group_and_dm_are_isolated_even_with_same_chat_id():
    resolver = IdentityResolver()
    group = resolver.resolve_thread("telegram", "tenant", "chat", kind="group")
    dm = resolver.resolve_thread("telegram", "tenant", "chat", kind="dm")
    assert group != dm
    assert group.kind == "group"
    assert dm.kind == "dm"


def test_thread_id_is_part_of_identity():
    resolver = IdentityResolver()
    assert resolver.resolve_thread("x", "s", "c", "1") != resolver.resolve_thread("x", "s", "c", "2")
    assert resolver.resolve_thread("x", "s", "c") != resolver.resolve_thread("x", "s", "c", "root")


def test_explicit_link_rejects_cross_channel_or_scope():
    resolver = IdentityResolver()
    principal = resolver.resolve_principal("telegram", "s", "u")
    thread = resolver.resolve_thread("discord", "s", "c")
    with pytest.raises(ValueError):
        resolver.link(principal, thread)


def test_resolve_returns_explicitly_linked_pair():
    principal, thread = IdentityResolver().resolve(
        channel="telegram", scope="workspace", external_user_id="u",
        external_chat_id="c", external_thread_id="t", kind="group")
    assert isinstance(principal, Principal)
    assert isinstance(thread, ConversationThread)
    assert (principal.data["channel"], principal.scope) == (thread.data["channel"], thread.scope)
