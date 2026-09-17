# progetti-vari

Standalone side projects. Each one lives in its own folder and builds independently.

## Aria — YouTube → MP3 / MP4

A small program for Windows that saves a YouTube video as an MP3 or an MP4.
Double-click, paste a link, press Download. No Python, no installation, no terminal.

**Download page for end users:** <https://andreimuresian.github.io/progetti-vari/>
**Direct download:** [Aria.exe](https://github.com/andreimuresian/progetti-vari/releases/latest/download/Aria.exe)

Source in [`aria/`](aria/) · build pipeline in [`.github/workflows/build-aria.yml`](.github/workflows/build-aria.yml)

### How it works

`Aria.exe` starts a web server on `127.0.0.1`, opens the default browser at it, and drives
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org) as subprocesses.
Everything runs on the user's own machine, so downloads go at their full connection speed and
nothing passes through a third-party server.

On first launch Aria downloads yt-dlp and ffmpeg into `%LOCALAPPDATA%\Aria\bin` (about a minute,
once). It then keeps yt-dlp updated by itself every few days, which is what stops a downloader
from quietly breaking when YouTube changes something.

### Repository setup (one time)

1. **Actions** — nothing to do. Pushing to `main` builds `Aria.exe` on a Windows runner,
   smoke-tests it, and publishes it to the release tagged `latest`.
2. **GitHub Pages** — Settings → Pages → Source: *Deploy from a branch*, branch `main`,
   folder `/docs`. That publishes the download page at the address above.
   (Skip this if you would rather send the Releases link directly — it works without Pages.)

### Licence

MIT, see [LICENSE](LICENSE). yt-dlp and ffmpeg are downloaded at runtime and keep their own
licences; they are not redistributed here.
