#!/usr/bin/env python3
"""
Standalone daily signals pipeline runner — same 11 steps the in-FastAPI
scheduler would run, but as a self-contained script you can launch from
launchd / cron / manually without needing uvicorn running.

Reuses HandelsregisterScheduler._SIGNALS_PIPELINE_STEPS so there is one
source of truth for the step list. Each step runs as its own subprocess
with a 15-minute timeout; one failing step doesn't halt the rest.

Usage:
    python3 scripts/run_pipeline.py                   # all 11 steps
    python3 scripts/run_pipeline.py --skip-external   # local-only (faster)
    python3 scripts/run_pipeline.py --only joiner     # just one step

Logs to data/pipeline.out.log (stdout) and data/pipeline.err.log (stderr)
when invoked under launchd. Exit codes:
    0 — all steps ran (even if some reported failures internally)
    2 — hard error (bad args, can't import scheduler module)
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scheduler.scheduler import HandelsregisterScheduler  # noqa: E402

logger = logging.getLogger(__name__)

STEPS = HandelsregisterScheduler._SIGNALS_PIPELINE_STEPS

# External steps that need network to third parties. --skip-external drops
# these for fast local runs. They stay in the daily cron.
_EXTERNAL_STEPS = {"source_crt_sh", "source_github", "source_cohorts", "source_hn_jobs", "sync_sheets"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--skip-external",
        action="store_true",
        help="Skip steps that reach external APIs (crt.sh, GitHub, cohort HTML, HN, Sheets)",
    )
    p.add_argument("--only", help="Run only the named step (e.g. 'joiner', 'adapt_companies')")
    p.add_argument("--step-timeout", type=int, default=900, help="Per-step timeout seconds (default 900 = 15 min)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.only:
        matching = [s for s in STEPS if s[0] == args.only]
        if not matching:
            logger.error("step %r not in pipeline. known steps: %s", args.only, [s[0] for s in STEPS])
            return 2
        steps_to_run = matching
    elif args.skip_external:
        steps_to_run = [s for s in STEPS if s[0] not in _EXTERNAL_STEPS]
    else:
        steps_to_run = list(STEPS)

    env = {**os.environ, "PYTHONPATH": str(project_root)}
    logger.info("running %d step(s)", len(steps_to_run))

    completed = failed = 0
    total_t0 = time.time()
    for step_id, cmd_args, desc in steps_to_run:
        cmd = [sys.executable, "-m", *cmd_args]
        t0 = time.time()
        logger.info("► %s: %s", step_id, desc)
        try:
            subprocess.run(cmd, check=True, timeout=args.step_timeout, cwd=str(project_root), env=env)
            elapsed = time.time() - t0
            logger.info("✓ %s (%.1fs)", step_id, elapsed)
            completed += 1
        except subprocess.TimeoutExpired:
            logger.error("✗ %s TIMEOUT after %ds", step_id, args.step_timeout)
            failed += 1
        except subprocess.CalledProcessError as e:
            logger.error("✗ %s exit=%d", step_id, e.returncode)
            failed += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("✗ %s unexpected: %s", step_id, e)
            failed += 1

    total_elapsed = time.time() - total_t0
    logger.info("pipeline done: %d ok / %d failed in %.0fs", completed, failed, total_elapsed)
    # Always exit 0 — launchd would otherwise mark the job as failed and
    # throttle subsequent runs. Individual step failures are logged above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
