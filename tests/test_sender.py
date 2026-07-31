import smtplib

from app import inbox, sender, templater
from app.models import Broker


class FakeSmtp:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.sent = []
        self.quit_called = False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


def _broker():
    return Broker(id=1, name="Spokeo", category="people-search", jurisdiction="US")


def test_mailto_link_unchanged():
    rendered = templater.RenderedRequest(subject="Subj [PIR-1]", body="Body\n", tag="PIR-1")
    link = sender.mailto_link("privacy@example.com", rendered)
    assert link == "mailto:privacy@example.com?subject=Subj%20%5BPIR-1%5D&body=Body%0A"


def test_smtp_config_defaults_when_table_missing():
    cfg = sender.SmtpConfig.from_dict({})
    assert cfg.host == ""
    assert cfg.port == 587
    assert cfg.use_ssl is False


def test_smtp_config_from_addr_defaults_to_username():
    cfg = sender.SmtpConfig.from_dict({"smtp": {"username": "me@example.com"}})
    assert cfg.from_addr == "me@example.com"


def test_smtp_config_ssl_security_sets_use_ssl():
    cfg = sender.SmtpConfig.from_dict({"smtp": {"security": "ssl"}})
    assert cfg.use_ssl is True


def test_build_message_headers():
    cfg = sender.SmtpConfig.from_dict({"smtp": {"username": "me@example.com"}})
    rendered = templater.RenderedRequest(subject="Subj [PIR-1]", body="Body text\n", tag="PIR-1")
    msg = sender.build_message(cfg, "privacy@example.com", rendered)
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "privacy@example.com"
    assert msg["Subject"] == "Subj [PIR-1]"
    assert msg.get_content() == "Body text\n"


def test_build_message_has_no_reply_to():
    cfg = sender.SmtpConfig.from_dict({"smtp": {"username": "me@example.com"}})
    rendered = templater.RenderedRequest(subject="Subj [PIR-1]", body="Body\n", tag="PIR-1")
    msg = sender.build_message(cfg, "privacy@example.com", rendered)
    assert msg["Reply-To"] is None


def test_subject_tag_survives_into_the_message(profile):
    cfg = sender.SmtpConfig.from_dict({"smtp": {"username": "me@example.com"}})
    rendered = templater.render(_broker(), profile, 42, "PIR")
    msg = sender.build_message(cfg, "privacy@example.com", rendered)
    assert inbox._request_id_from_subject(msg["Subject"], "PIR") == 42


def test_open_smtp_starttls_then_login(monkeypatch):
    fake = None

    def factory(host, port):
        nonlocal fake
        fake = FakeSmtp(host, port)
        return fake

    monkeypatch.setattr(smtplib, "SMTP", factory)
    cfg = sender.SmtpConfig.from_dict({
        "smtp": {"host": "smtp.example.com", "port": 587, "username": "u", "password": "p"},
    })
    client = sender.open_smtp(cfg)
    assert client is fake
    assert fake.started_tls is True
    assert fake.login_args == ("u", "p")


def test_open_smtp_ssl_skips_starttls(monkeypatch):
    fake = None

    def factory(host, port):
        nonlocal fake
        fake = FakeSmtp(host, port)
        return fake

    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    cfg = sender.SmtpConfig.from_dict({
        "smtp": {"host": "smtp.example.com", "port": 465, "username": "u", "password": "p", "security": "ssl"},
    })
    client = sender.open_smtp(cfg)
    assert client is fake
    assert fake.started_tls is False
    assert fake.login_args == ("u", "p")
