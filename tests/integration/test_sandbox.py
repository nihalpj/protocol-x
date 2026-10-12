"""AC-P1-02/03/05: sandbox dispatch, timeout kill, OOM crash mapping."""

from __future__ import annotations

import time

import pytest

from adapters.base import SANDBOX_CRASH, SANDBOX_TIMEOUT, ToolError
from core.sandbox.runner import CallRequest, SubprocessRunner


@pytest.fixture()
def runner():
    return SubprocessRunner()


def _req(tool: str, args: dict, timeout: int = 30) -> CallRequest:
    return CallRequest(
        adapter_module="_test_bombs",
        tool_name=tool,
        args=args,
        workspace="/tmp",
        timeout_s=timeout,
    )


def test_timeout_kills_at_wall_clock(runner):
    start = time.monotonic()
    with pytest.raises(ToolError) as exc:
        runner.run(_req("test_sleeper", {"seconds": 60}, timeout=30))
    elapsed = time.monotonic() - start
    assert exc.value.code == SANDBOX_TIMEOUT
    assert elapsed <= 31
    assert "Traceback" not in exc.value.message


def test_memory_bomb_is_sandbox_crash(runner):
    with pytest.raises(ToolError) as exc:
        runner.run(_req("test_memory_hog", {}, timeout=60))
    assert exc.value.code == SANDBOX_CRASH


def test_nonzero_exit_is_sandbox_crash(runner):
    with pytest.raises(ToolError) as exc:
        runner.run(_req("test_crasher", {}))
    assert exc.value.code == SANDBOX_CRASH


def test_server_keeps_serving_after_crash(ctx, runner, sample_png):
    with pytest.raises(ToolError):
        runner.run(_req("test_crasher", {}))
    result = ctx.dispatch(
        "image_resize", {"path": sample_png, "width": 800, "height": 600}
    )
    assert result["result"]["data"]["output"]["size_bytes"] > 0


def test_dispatch_validation_error(ctx):
    from adapters.base import VALIDATION_ERROR

    with pytest.raises(ToolError) as exc:
        ctx.dispatch("image_resize", {"path": "x.png", "width": 1, "height": 1})
    assert exc.value.code == VALIDATION_ERROR  # missing file, before decode
