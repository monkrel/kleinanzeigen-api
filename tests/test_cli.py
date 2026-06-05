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
