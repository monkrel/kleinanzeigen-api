"""CLI behavior: clean exit codes/messages for bad input and API failures."""

from kleinanzeigen_api import cli


def test_cli_categories_browser_is_offline(capsys):
    rc = cli.main(["--categories", "autos"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "216" in out  # Autos


def test_cli_unknown_category_exits_2(capsys):
    # resolve_category raises ValueError before any network call
    rc = cli.main(["--category", "nonsense-xyz", "--q", "x"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_runtime_error_is_clean(monkeypatch, capsys):
    """A rotated-credential or network error should print 'error: ...' and exit
    with a nonzero code, not show a traceback."""

    class Boom:
        def __init__(self, **kw):
            pass

        def search(self, **kw):
            raise RuntimeError("401 from API — credentials likely rotated")

    monkeypatch.setattr(cli, "KleinanzeigenAPI", Boom)
    rc = cli.main(["Berlin", "--q", "x"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "error:" in err and "401" in err


def test_cli_categories_query_not_found(capsys):
    rc = cli.main(["--categories", "nonexistent-category-query-xyz"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no categories match" in err


def test_cli_search_json_and_out(monkeypatch, capsys, tmp_path):
    from kleinanzeigen_api import Listing

    sample = Listing(
        id="500",
        title="Test Fahrrad",
        description="Tolles Fahrrad",
        price=150.0,
        price_type="FIXED",
        url="https://kleinanzeigen.de/s-500",
        city="Berlin",
        zip_code="10115",
        latitude=52.52,
        longitude=13.40,
        size_m2=None,
        rooms=None,
        posted="2026-07-10",
        poster_type="PRIVATE",
    )

    class FakeAPI:
        def __init__(self, **kw):
            pass

        def search(self, **kw):
            return [sample]

    monkeypatch.setattr(cli, "KleinanzeigenAPI", FakeAPI)

    # Test table output
    rc = cli.main(["Berlin", "--q", "Fahrrad"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[500]" in out
    assert "Test Fahrrad" in out

    # Test --json flag
    rc = cli.main(["Berlin", "--q", "Fahrrad", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"id": "500"' in out

    # Test --out flag
    out_file = tmp_path / "results.json"
    rc = cli.main(["Berlin", "--q", "Fahrrad", "--out", str(out_file)])
    assert rc == 0
    assert "wrote 1 listings" in capsys.readouterr().err
    assert '"id": "500"' in out_file.read_text(encoding="utf-8")


def test_cli_not_logged_in_exits_2(monkeypatch, capsys):
    import pytest
    from kleinanzeigen_api.auth import Authenticator

    monkeypatch.setattr(Authenticator, "logged_in", False)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["chats"])
    assert excinfo.value.code == 2
    assert "Not logged in" in capsys.readouterr().err


def test_cli_subcommands(monkeypatch, capsys):
    from kleinanzeigen_api import Conversation, Listing

    conv = Conversation(
        id="chat-1",
        ad_id="ad-1",
        ad_title="Tisch",
        role="BUYER",
        counterparty="Bob",
        unread=True,
        unread_count=1,
        last_received="2026-07-10",
        preview="Hallo",
    )
    listing = Listing(
        id="ad-1",
        title="Tisch",
        description="Holztisch",
        price=20.0,
        price_type="FIXED",
        url="https://kleinanzeigen.de/ad-1",
        city="Berlin",
        zip_code="10115",
        latitude=None,
        longitude=None,
        size_m2=None,
        rooms=None,
        posted="2026-07-10",
        poster_type="PRIVATE",
    )

    class FakeAuthedAPI:
        def conversations(self):
            return [conv]

        def messages(self, cid):
            return [{"direction": "received", "date": "10:00", "text": "Hallo"}]

        def reply(self, cid, text):
            pass

        def my_ads():
            return [listing]

        def watchlist(self):
            return [listing]

        def pause_ad(self, ad_id):
            pass

        def activate_ad(self, ad_id):
            pass

        def delete_ad(self, ad_id):
            pass

        def extend_ad(self, ad_id):
            pass

        def best_location(self, loc):
            return [("3331", "Berlin")]

        def post_ad(self, **kw):
            return "new-ad-999"

    fake_api = FakeAuthedAPI()
    fake_api.my_ads = lambda: [listing]
    monkeypatch.setattr(cli, "_authed_client", lambda rate=1.5: fake_api)

    # chats
    assert cli.main(["chats"]) == 0
    out = capsys.readouterr().out
    assert "● [chat-1] Bob — Tisch" in out
    assert cli.main(["chats", "--json"]) == 0
    assert "chat-1" in capsys.readouterr().out

    # messages
    assert cli.main(["messages", "chat-1"]) == 0
    assert "→ you [10:00]: Hallo" in capsys.readouterr().out
    assert cli.main(["messages", "chat-1", "--json"]) == 0
    assert '"text": "Hallo"' in capsys.readouterr().out

    # reply
    assert cli.main(["reply", "chat-1", "Danke"]) == 0
    assert "sent" in capsys.readouterr().err

    # my-ads & watchlist
    assert cli.main(["my-ads"]) == 0
    assert "Tisch" in capsys.readouterr().out
    assert cli.main(["watchlist"]) == 0
    assert "Tisch" in capsys.readouterr().out

    # post
    assert (
        cli.main(
            [
                "post",
                "--title",
                "Sofa",
                "--description",
                "Neu",
                "--category",
                "216",
                "--location",
                "Berlin",
            ]
        )
        == 0
    )
    assert "posted ad new-ad-999" in capsys.readouterr().err

    # pause / activate / delete / extend
    assert cli.main(["pause", "123"]) == 0
    assert cli.main(["activate", "123"]) == 0
    assert cli.main(["delete", "123"]) == 0
    assert cli.main(["extend", "123"]) == 0
