from typer.testing import CliRunner

from parking import __version__
from parking.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"parking {__version__}"
    assert __version__ == "0.1.0"


def test_sub_apps_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("models", "worker", "db", "push", "admin"):
        assert name in result.output
