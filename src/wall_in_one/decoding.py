"""How ffmpeg is told to open a file that somebody else wrote.

A wallpaper, a Workshop item's video or preview, a download: all of them are
bytes an author chose. ffmpeg decides what a file is from its contents, not its
name, so a playlist named ``.gif`` or ``.mp4`` (an HLS playlist, an ``ffconcat``
script) makes it open whatever that playlist names: other local files, or the
network. Every decode of such a file puts `input_options` before ``-i``:

- ``-protocol_whitelist file`` (and ``pipe`` for a download handed over stdin):
  no network, no other protocol, however the input is spelled;
- ``-format_whitelist``: only the picture and video containers a wallpaper is
  (`FORMATS`), which leaves out the demuxers that follow references
  (``hls``, ``concat``, ``dash``, ``image2pipe`` lists...).

ffmpeg reads more into a name than the file, too: its ``image2`` demuxer takes
``preview%01d.png`` for a numbered sequence and opens ``preview0.png`` instead,
which may be a link out of the folder that was checked (R42-10). So it is never
handed a name: the input is the file opened (`opened`), and ``image2`` is not in
`FORMATS`.

A file that is none of these is refused ("Invalid argument"), which is a card
without a picture, not an error worth a dialog.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from wall_in_one import file_io

#: The demuxers a wallpaper's picture or video can be, by the names ffmpeg gives
#: them (a name with aliases, ``matroska,webm``, matches by any one of them). The
#: bytes choose among them: the input is a descriptor's path (`opened`), which has
#: no extension to go by.
FORMATS: Final = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpegts,mpeg,asf,ogg,ogv,ivf,m4v,h264,hevc,"
    "gif,apng,png_pipe,jpeg_pipe,webp_pipe,webp_anim,bmp_pipe,tiff_pipe,jpegxl_pipe,jpegxl_anim"
)


def input_options(*, pipe: bool = False) -> list[str]:
    """The options that go before ``-i``, ``pipe`` when the input is standard input."""
    return [
        "-protocol_whitelist",
        "file,pipe" if pipe else "file",
        "-format_whitelist",
        FORMATS,
    ]


@contextlib.contextmanager
def opened(path: Path, *, follow: bool = False) -> Iterator[Path]:
    """``path`` as ffmpeg's input: the regular file it names now, opened, by its descriptor.

    The path given to ffmpeg is ``/proc/<pid>/fd/<n>``: exactly the inode opened
    here, whatever is at the name by the time ffmpeg runs, and a name with no
    pattern, glob or extension in it to be read as anything else. A symbolic
    link at ``path`` itself is refused (the library's walk lists none), unless
    ``follow``, as is anything but a regular file: an `OSError`, as when it
    can't be opened. So is a link in place of the folder it is in, a Workshop
    item's swapped for one since the scan (f7 R42-18): that folder is opened
    without following one, and the file in it; the folders above it are
    followed (a library folder reached through a link). ``follow`` is for a
    scene's paired still only, which playback follows too (`thumbnails.generate`).
    """
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    if follow:
        descriptor = os.open(path, flags & ~os.O_NOFOLLOW)
    else:
        folder = file_io.pin_directory_path(path.parent)
        try:
            descriptor = os.open(path.name, flags, dir_fd=folder.descriptor)
        finally:
            folder.close()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        yield Path("/proc") / str(os.getpid()) / "fd" / str(descriptor)
    finally:
        os.close(descriptor)
