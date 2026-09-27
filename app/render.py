"""Render an email's HTML to a screenshot, so the offer inside the artwork can be read.

Retailer marketing email puts the numbers in pictures. On a live Good Guys email the
extracted text contains "20%" but not "Ends", not "13/09/2026" and not "11.59" - those
are pixels. No amount of parsing gets them; a render does.

Two things this cannot do, both learned by trying:

* **It only works while the offer is live.** The hero images are served from a live URL
  and swapped when the offer ends, so rendering an old email shows "This offer has
  ended" rather than what it once said. Useless for a backfill, fine for a daily job.
* **It cannot avoid the tracking pixel.** The images carry the offer, so they must load,
  and loading them tells the retailer the mail was opened.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

log = logging.getLogger("shopwatch.render")

#: Headless Chrome in a container: nothing to install on the host, and it cannot see
#: anything outside the directory handed to it.
CHROME_IMAGE = os.environ.get("SHOPWATCH_CHROME_IMAGE", "zenika/alpine-chrome:latest")

#: Tall enough for the hero artwork and the first screen of fine print, which is where
#: the amount and the expiry live. Rendering 6,000px of product grid buys nothing and
#: costs tokens.
WIDTH = int(os.environ.get("SHOPWATCH_RENDER_WIDTH", "900"))
HEIGHT = int(os.environ.get("SHOPWATCH_RENDER_HEIGHT", "3200"))

#: How long Chrome itself waits for the page before taking the screenshot anyway.
#: Marketing email carries tracking pixels and image hosts that never answer, and
#: Chrome's default is to wait for every one of them. Without this, a single dead
#: resource ran the render into the subprocess timeout (120s, seen on opti 24 and 26
#: Sep 2026) and the offer fell back to the text pass, which is exactly the case the
#: render exists for. Measured on opti 27 Sep 2026: an <img> pointing at a port that
#: drops the SYN never produced a screenshot in 130s without this flag, and produced
#: one at 45s with it. `--virtual-time-budget` did NOT help; only `--timeout` did.
CHROME_TIMEOUT_MS = int(os.environ.get("SHOPWATCH_CHROME_TIMEOUT_MS", "45000"))

#: The offer artwork is often taller than one screen. Chrome's --screenshot only ever
#: captures the top WIDTHxHEIGHT of the page, so the page is rendered again with the
#: document shifted up by one screen per tile (a negative top margin injected ahead of
#: the email's own markup). The Good Guys "Just For You" mail of 24 Sep 2026 put the
#: code in the first screen and the full exclusion list at about 4,000px, below it.
TILES = int(os.environ.get("SHOPWATCH_RENDER_TILES", "2"))

#: A tile that is (nearly) blank is not worth a vision call. A blank 900x3200 PNG from
#: this Chrome build weighs about 18KB; a tile carrying artwork weighs hundreds of KB.
BLANK_TILE_BYTES = int(os.environ.get("SHOPWATCH_RENDER_BLANK_BYTES", "30000"))

#: Chrome's flags. `--disable-dev-shm-usage` is not optional: Docker gives the container
#: a 64MB /dev/shm and Chrome's GPU process crashes in it on image-heavy mail, exiting 0
#: with no screenshot. Measured on opti 27 Sep 2026 against The Good Guys "Just For You"
#: email, five renders each: 3/5 without the flag ("Reinitialized the GPU process after a
#: crash"), 5/5 with it. The alternative, `docker run --shm-size=512m`, also scored 5/5
#: but needs a docker-level option; the Chrome flag needs nothing from the host.
CHROME_FLAGS = (
    "--no-sandbox", "--headless", "--disable-gpu", "--disable-dev-shm-usage",
    "--hide-scrollbars",
)

#: A render that exits cleanly with no file is a crash, not a verdict, and a crash is
#: not deterministic. One more go before giving up costs a few seconds.
ATTEMPTS = 2


class RenderError(RuntimeError):
    """Rendering failed. The caller keeps whatever the text pass produced."""


def available() -> bool:
    """Is there a docker to render with? Checked so the caller can skip quietly."""
    return shutil.which("docker") is not None


def _force_remove(name: str) -> None:
    """Kill and remove the render container, if it is still there.

    THE TIMEOUT DOES NOT STOP THE CONTAINER. `subprocess.run(timeout=...)` kills
    the `docker run` CLIENT, which is only talking to the daemon over a socket.
    The container carries on, and `--rm` never fires because `--rm` runs when the
    container EXITS. So a marketing email whose hero images hang left a headless
    Chrome running with network access, permanently, with its bind-mount source
    already deleted by the cleanup below.

    Seen on opti 2026-09-18: the daily mailwatch run reported "errors 0" and left
    `intelligent_grothendieck` running for ten minutes and counting, still holding
    the outbound connection that tripped egress-watch. That alert is what surfaced
    it; nothing in shopwatch noticed.

    Normally there is nothing to remove, because the happy path exited and `--rm`
    already cleaned up. "No such container" is therefore the EXPECTED answer here
    and is not worth a line in the log, which is why the output is discarded
    rather than swallowed blindly: the failure this could hide (a container that
    will not die) shows up as a second render failing, and as a container Docker
    refuses to start under a name already in use.
    """
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        # Deliberately not fatal: we are already on a failure path and raising
        # here would replace the render error with a cleanup error, losing the
        # reason the render failed in the first place.
        log.warning("could not remove render container %s: %s", name, exc)


def shifted(html: str, offset: int) -> str:
    """The email's HTML with the document pulled up by `offset` pixels, so that a
    fixed-size screenshot shows the NEXT screen of it. Unchanged when offset is 0.

    Injected ahead of the markup rather than into <head>, because marketing email
    is not reliably well-formed and a style block at the top is honoured either way.
    """
    if offset <= 0:
        return html
    return f"<style>html{{margin-top:-{int(offset)}px !important}}</style>" + html


def render_html(html: str, timeout: int = 120, offset: int = 0) -> Path:
    """Render HTML to a PNG and return its path. The caller owns the file.

    `offset` is how many pixels of the page to skip before the screenshot starts:
    0 for the first screen, HEIGHT for the second, and so on (see `render_tiles`).

    A run that ends with no screenshot is retried once (see ATTEMPTS); a timeout or
    any other failure is not, because those are not the flaky case and the caller's
    own budget is already spent on them.
    """
    if not available():
        raise RenderError("docker is not available on this machine")
    last: RenderError | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _render_once(html, timeout, offset)
        except RenderError as exc:
            if "no screenshot produced" not in str(exc) or attempt == ATTEMPTS:
                raise
            last = exc
            log.warning("render attempt %d produced no screenshot, retrying: %s",
                        attempt, exc)
    raise last  # pragma: no cover - the loop always returns or raises


def _render_once(html: str, timeout: int, offset: int) -> Path:
    """One docker run. The work directory is world-writable on purpose: the container
    runs as its own user and has to write the screenshot back out. It holds one email
    for a few seconds and is removed by the caller.
    """
    workdir = Path(tempfile.mkdtemp(prefix="shopwatch-render-"))
    # Named, because the container has to be killable by something other than the
    # `docker run` process. Without a name, Docker invents one and the only handle
    # on a container we have lost track of is a guess.
    name = f"shopwatch-render-{uuid.uuid4().hex[:12]}"
    try:
        os.chmod(workdir, 0o777)
        (workdir / "email.html").write_text(shifted(html, offset), encoding="utf-8",
                                            errors="replace")
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--name", name,
                "-v", f"{workdir}:/data",
                CHROME_IMAGE,
                *CHROME_FLAGS,
                f"--timeout={CHROME_TIMEOUT_MS}",
                f"--window-size={WIDTH},{HEIGHT}",
                "--screenshot=/data/shot.png",
                "file:///data/email.html",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _force_remove(name)
        shutil.rmtree(workdir, ignore_errors=True)
        raise RenderError(f"chrome timed out after {timeout}s") from exc
    except Exception as exc:
        _force_remove(name)
        shutil.rmtree(workdir, ignore_errors=True)
        raise RenderError(f"{type(exc).__name__}: {exc}") from exc

    shot = workdir / "shot.png"
    if not shot.exists() or shot.stat().st_size == 0:
        shutil.rmtree(workdir, ignore_errors=True)
        # Chrome writes its real complaint to stderr amid a wall of GPU warnings.
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise RenderError(f"no screenshot produced: {tail[0][:160]}")
    log.debug("rendered %d bytes to %s", shot.stat().st_size, shot)
    return shot


def render_tiles(html: str, timeout: int = 120, tiles: int = TILES,
                 min_bytes: int = BLANK_TILE_BYTES) -> list[Path]:
    """Render the first `tiles` screens of the email, top down, as separate PNGs.

    Stops early at the first screen that comes back (nearly) blank, because
    everything below a blank screen is blank too and a vision call on white space
    buys nothing. The first tile is never skipped: if it is blank, that is a fact
    about the email and the model gets to say so.

    A failure on a LATER tile does not throw away the earlier ones: the first screen
    alone recovered the code on the Good Guys mail, and a caller that lost it because
    the second screen timed out would be worse off than one that never looked.
    """
    shots: list[Path] = []
    for index in range(max(1, tiles)):
        try:
            shot = render_html(html, timeout=timeout, offset=index * HEIGHT)
        except RenderError:
            if not shots:
                raise
            log.warning("tile %d failed to render; keeping the %d before it",
                        index, len(shots))
            break
        if index > 0 and shot.stat().st_size < min_bytes:
            log.debug("tile %d is blank (%d bytes); stopping", index, shot.stat().st_size)
            cleanup(shot)
            break
        shots.append(shot)
    return shots


def cleanup(shot: Path | list[Path]) -> None:
    """Remove the screenshot(s) and their directories. One-use artefacts, gone when read."""
    for one in (shot if isinstance(shot, list) else [shot]):
        try:
            shutil.rmtree(one.parent, ignore_errors=True)
        except Exception:
            pass
