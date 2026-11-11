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

A file that is none of these is refused ("Invalid argument"), which is a card
without a picture, not an error worth a dialog.
"""

from __future__ import annotations

from typing import Final

#: The demuxers a wallpaper's picture or video can be, by the names ffmpeg gives
#: them (a name with aliases, ``matroska,webm``, matches by any one of them).
FORMATS: Final = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpegts,mpeg,asf,ogg,ogv,ivf,m4v,h264,hevc,"
    "gif,apng,png_pipe,jpeg_pipe,webp_pipe,webp_anim,bmp_pipe,tiff_pipe,jpegxl_pipe,jpegxl_anim,image2"
)


def input_options(*, pipe: bool = False) -> list[str]:
    """The options that go before ``-i``, ``pipe`` when the input is standard input."""
    return [
        "-protocol_whitelist",
        "file,pipe" if pipe else "file",
        "-format_whitelist",
        FORMATS,
    ]
