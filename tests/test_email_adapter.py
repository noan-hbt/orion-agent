from __future__ import annotations

import threading
import time
from email.message import EmailMessage

import channel_adapters
from channel_adapters import EmailAdapter
from orion_config import OrionConfig


def _raw_mail(sender: str, *, subject: str = "hello", body: str = "body") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "orion@example.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{sender}-{subject}@test>"
    message.set_content(body)
    return message.as_bytes()


class _Mailbox:
    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages
        self.seen: set[bytes] = set()
        self.fetch_specs: list[str] = []
        self.store_calls: list[bytes] = []

    def connect(self, *_args, **_kwargs):
        mailbox = self

        class Connection:
            def login(self, *_args):
                return "OK", []

            def select(self, *_args):
                return "OK", []

            def search(self, *_args):
                unseen = [ident for ident in mailbox.messages if ident not in mailbox.seen]
                return "OK", [b" ".join(unseen)]

            def fetch(self, message_id, spec):
                mailbox.fetch_specs.append(spec)
                raw = mailbox.messages.get(message_id)
                if raw is None:
                    return "NO", []
                return "OK", [(b"RFC822", raw)]

            def store(self, message_id, *_args):
                mailbox.store_calls.append(message_id)
                mailbox.seen.add(message_id)
                return "OK", []

            def logout(self):
                return "BYE", []

        return Connection()


def _adapter(**kwargs) -> EmailAdapter:
    return EmailAdapter(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="orion@example.com",
        password="secret",
        poll_interval=0.01,
        **kwargs,
    )


def _start_worker(adapter: EmailAdapter, callback) -> threading.Thread:
    adapter._on_message = callback
    adapter._stop_requested.clear()
    worker = threading.Thread(target=adapter._worker_loop, daemon=True)
    adapter._worker = worker
    worker.start()
    return worker


def test_callback_failure_leaves_unseen_and_next_poll_retries(monkeypatch):
    mailbox = _Mailbox({b"1": _raw_mail("owner@example.com")})
    monkeypatch.setattr(channel_adapters.imaplib, "IMAP4_SSL", mailbox.connect)
    adapter = _adapter(allowed_senders=["owner@example.com"])
    calls = []

    def callback(message):
        calls.append(message.payload["from"])
        if len(calls) == 1:
            raise RuntimeError("temporary")

    _start_worker(adapter, callback)
    try:
        adapter._poll_once()
        assert calls == ["owner@example.com"]
        assert mailbox.seen == set()

        adapter._poll_once()
        assert calls == ["owner@example.com", "owner@example.com"]
        assert mailbox.seen == {b"1"}
    finally:
        adapter.stop()


def test_seen_is_written_only_after_callback_success(monkeypatch):
    mailbox = _Mailbox({b"1": _raw_mail("owner@example.com")})
    monkeypatch.setattr(channel_adapters.imaplib, "IMAP4_SSL", mailbox.connect)
    adapter = _adapter(allowed_senders=["owner@example.com"])
    entered = threading.Event()
    release = threading.Event()

    def callback(_message):
        entered.set()
        assert release.wait(timeout=2.0)

    _start_worker(adapter, callback)
    poller = threading.Thread(target=adapter._poll_once, daemon=True)
    poller.start()
    try:
        assert entered.wait(timeout=2.0)
        assert mailbox.seen == set()
        assert mailbox.store_calls == []
        release.set()
        poller.join(timeout=2.0)
        assert not poller.is_alive()
        assert mailbox.seen == {b"1"}
        assert mailbox.fetch_specs == ["(BODY.PEEK[])"]
    finally:
        release.set()
        adapter.stop()


def test_sender_allowlist_filters_untrusted_mail_and_absent_is_fail_closed(monkeypatch):
    mailbox = _Mailbox(
        {
            b"1": _raw_mail("attacker@example.net", subject="bad"),
            b"2": _raw_mail("Owner <OWNER@example.com>", subject="good"),
        }
    )
    monkeypatch.setattr(channel_adapters.imaplib, "IMAP4_SSL", mailbox.connect)
    adapter = _adapter(allowed_senders=["owner@example.com"])
    received = []
    _start_worker(adapter, lambda message: received.append(message.payload["from"]))
    try:
        adapter._poll_once()
        assert received == ["OWNER@example.com"]
        assert mailbox.seen == {b"1", b"2"}
    finally:
        adapter.stop()

    legacy = _adapter()
    assert legacy._sender_allowed("anyone@anywhere.test") is False
    assert legacy.allowed_senders == set()
    locked = _adapter(allowed_senders=[])
    assert locked._sender_allowed("owner@example.com") is False


def test_manual_email_adapter_without_sender_allowlist_never_dispatches(monkeypatch):
    mailbox = _Mailbox({b"1": _raw_mail("attacker@example.net")})
    monkeypatch.setattr(channel_adapters.imaplib, "IMAP4_SSL", mailbox.connect)
    adapter = _adapter()
    received = []
    adapter._on_message = received.append

    adapter._poll_once()

    assert received == []
    assert adapter._queue.empty()
    assert mailbox.seen == {b"1"}


def test_email_config_wires_sender_allowlist(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    config = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["email"],
                "email": {
                    "imap_host": "imap.example.com",
                    "smtp_host": "smtp.example.com",
                    "username": "orion@example.com",
                    "allowed_senders": ["owner@example.com"],
                    "allowed_recipient_domains": ["example.com"],
                },
            }
        }
    )

    class Router:
        def __init__(self):
            self.registered = []

        def register(self, adapter):
            self.registered.append(adapter)

    router = Router()
    config._configure_channels(router)

    assert len(router.registered) == 1
    assert router.registered[0].allowed_senders == {"owner@example.com"}
    assert router.registered[0].allowed_recipient_domains == {"example.com"}


def test_legacy_email_config_without_allowed_senders_wires_fail_closed(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    config = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["email"],
                "email": {
                    "imap_host": "imap.example.com",
                    "smtp_host": "smtp.example.com",
                    "username": "orion@example.com",
                },
            }
        }
    )

    class Router:
        def __init__(self):
            self.registered = []

        def register(self, adapter):
            self.registered.append(adapter)

    router = Router()
    config._configure_channels(router)

    adapter = router.registered[0]
    assert adapter.allowed_senders == set()
    assert adapter._sender_allowed("attacker@example.net") is False


def test_stop_waits_for_accepted_callback_and_marks_seen(monkeypatch):
    mailbox = _Mailbox({b"1": _raw_mail("owner@example.com")})
    monkeypatch.setattr(channel_adapters.imaplib, "IMAP4_SSL", mailbox.connect)
    adapter = _adapter(allowed_senders=["owner@example.com"])
    entered = threading.Event()
    release = threading.Event()

    def callback(_message):
        entered.set()
        assert release.wait(timeout=2.0)

    adapter.start(callback)
    assert entered.wait(timeout=2.0)

    stopped = threading.Event()

    def do_stop():
        adapter.stop()
        stopped.set()

    stopper = threading.Thread(target=do_stop, daemon=True)
    stopper.start()
    time.sleep(0.05)
    assert not stopped.is_set()
    assert mailbox.seen == set()

    release.set()
    stopper.join(timeout=2.0)
    assert stopped.is_set()
    assert mailbox.seen == {b"1"}

    # Repeated lifecycle shutdown stays harmless and cannot strand a sentinel.
    adapter.stop()
