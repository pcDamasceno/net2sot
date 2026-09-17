from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nornir.core.task import Result

from net2sot.push_config import build_parser, push_rendered_config


def test_defaults_to_dry_run_merge():
    args = build_parser().parse_args(["--filter-tag", "monitoring"])

    assert args.dry_run is True
    assert args.replace is False


def test_apply_replace_flags_are_explicit():
    args = build_parser().parse_args(
        ["--filter-device", "R1", "--apply", "--replace"]
    )

    assert args.dry_run is False
    assert args.replace is True


def test_pushes_rendered_content_to_napalm():
    host = MagicMock()
    host.name = "R1"
    task = MagicMock()
    task.host = host
    nb = MagicMock()
    nb.get_device.return_value = SimpleNamespace(id=42)
    nb.get_rendered_config.return_value = "hostname R1\n"

    with patch("net2sot.push_config.napalm_configure") as configure:
        configure.return_value = Result(host=host, changed=True, diff="candidate diff")
        result = push_rendered_config(
            task,
            nb,
            dry_run=True,
            replace=False,
            commit_message="preview",
        )

    nb.get_rendered_config.assert_called_once_with(42)
    assert configure.call_args.args == (task,)
    assert configure.call_args.kwargs == {
        "configuration": "hostname R1\n",
        "dry_run": True,
        "replace": False,
        "commit_message": "preview",
    }
    assert result.changed is True
    assert result.diff == "candidate diff"
    assert result.failed is False


def test_render_failure_does_not_contact_device():
    host = MagicMock()
    host.name = "R1"
    task = MagicMock()
    task.host = host
    nb = MagicMock()
    nb.get_device.return_value = SimpleNamespace(id=42)
    nb.get_rendered_config.side_effect = RuntimeError("render failed")

    result = push_rendered_config(task, nb, dry_run=True, replace=False)

    assert result.failed is True
    task.run.assert_not_called()


def test_napalm_connection_failure_is_returned_without_exception():
    host = MagicMock()
    host.name = "R1"
    task = MagicMock()
    task.host = host
    nb = MagicMock()
    nb.get_device.return_value = SimpleNamespace(id=42)
    nb.get_rendered_config.return_value = "hostname R1\n"

    with patch(
        "net2sot.push_config.napalm_configure",
        side_effect=ConnectionError(
            "Unauthorized.\nUnable to authenticate user: Bad username/password combination"
        ),
    ):
        result = push_rendered_config(task, nb, dry_run=True, replace=False)

    assert result.failed is True
    assert result.exception is None
    assert result.result == (
        "Device connection/configuration failed: Unauthorized. Unable to authenticate "
        "user: Bad username/password combination"
    )
