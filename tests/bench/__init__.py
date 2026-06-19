"""Live, on-demand benchmark: which NVIDIA model best fits each graph LLM-call.

Not collected by CI (this package's modules are not named ``test_*``). The offline harness
unit test lives at ``tests/test_bench_harness.py`` and uses a fake model -- it pays for no
live calls. Run the real benchmark with::

    uv run python -m tests.bench.nvidia_role_fit            # full matrix
    uv run python -m tests.bench.nvidia_role_fit --dry-run  # list cells, fire nothing
"""
