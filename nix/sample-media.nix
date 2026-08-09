{ pkgs }:

pkgs.runCommand "wall-in-one-vm-sample-media"
  {
    nativeBuildInputs = [ pkgs.ffmpeg ];
  }
  ''
    mkdir -p "$out"

    ffmpeg -hide_banner -loglevel error -nostdin -threads 1 \
      -f lavfi -i "testsrc2=size=1280x720:rate=1" \
      -frames:v 1 "$out/colour-grid.png"
    ffmpeg -hide_banner -loglevel error -nostdin -threads 1 \
      -f lavfi -i "smptebars=size=1280x720:rate=1" \
      -frames:v 1 "$out/colour-bars.png"
    ffmpeg -hide_banner -loglevel error -nostdin -threads 1 \
      -f lavfi -i "color=c=0x102040:size=1280x720:rate=1" \
      -vf "drawgrid=width=80:height=80:thickness=2:color=0x40a0ff@0.8" \
      -frames:v 1 "$out/night-grid.png"
    ffmpeg -hide_banner -loglevel error -nostdin -threads 1 \
      -f lavfi -i "testsrc2=size=1280x720:rate=24" \
      -t 4 -c:v mpeg4 -q:v 4 -pix_fmt yuv420p \
      "$out/moving-grid.mp4"
  ''
