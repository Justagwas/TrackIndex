# TrackIndex

<div align="center">

[![Code: GitHub](https://img.shields.io/badge/Code-GitHub-111827.svg?style=flat&logo=github&logoColor=white)](https://github.com/Justagwas/TrackIndex)
[![Website](https://img.shields.io/badge/Website-TrackIndex-0ea5e9.svg?style=flat&logo=google-chrome&logoColor=white)](https://www.justagwas.com/projects/trackindex)
[![Mirror: SourceForge](https://img.shields.io/badge/Mirror-SourceForge-ff6600.svg?style=flat&logo=sourceforge&logoColor=white)](https://sourceforge.net/projects/trackindex/)

</div>

<p align="center">
  <img
    width="128"
    height="128"
    alt="TrackIndex Logo"
    src="TrackIndex/icon.ico"
  />
</p>

<div align="center">

[![Download (Windows)](https://img.shields.io/badge/Download-Windows%20(TrackIndexSetup.exe)-2563eb.svg?style=flat&logo=windows&logoColor=white)](https://github.com/Justagwas/TrackIndex/releases/latest/download/TrackIndexSetup.exe)

</div>

<p align="center"><b>Your Local Music Library Manager & Player</b></p>

<p align="center">Windows music library organizer and local audio player that keeps playlist order synchronized</p>

<div align="center">

[![Version](https://img.shields.io/github/v/tag/Justagwas/TrackIndex.svg?label=Version)](https://github.com/Justagwas/TrackIndex/tags)
[![License](https://img.shields.io/github/license/Justagwas/TrackIndex.svg)](https://github.com/Justagwas/TrackIndex/blob/main/LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/Justagwas/TrackIndex/main.svg?style=flat&cacheSeconds=3600)](https://github.com/Justagwas/TrackIndex/commits/main)
[![Open Issues](https://img.shields.io/github/issues/Justagwas/TrackIndex.svg)](https://github.com/Justagwas/TrackIndex/issues)
[![Stars](https://img.shields.io/github/stars/Justagwas/TrackIndex.svg?style=flat&cacheSeconds=3600)](https://github.com/Justagwas/TrackIndex/stargazers)
![Downloads (7d)](https://img.shields.io/badge/dynamic/json?style=flat&url=https%3A%2F%2Fdownload-stats-worker.justagwas.workers.dev%2Fdownloads%2Ftrackindex%3Frange%3Dweek&query=%24.data.weekly.all&label=Downloads%20(7d))

</div>

## Overview

TrackIndex is a Windows application for organizing and playing music stored in local folders. Add a folder as a library, arrange its tracks in the order you want, and listen without moving between a file manager, playlist editor, and separate music player.

TrackIndex can keep numbered filenames and a chosen M3U or M3U8 playlist aligned with the order on screen, helping the collection remain useful across different players and devices. Multiple libraries stay close at hand, while artwork and music details make each folder pleasant to browse. Everything happens locally without an account or library upload, and the tags inside your audio files remain untouched.

## Basic usage

1. Download and install from the [latest release](https://github.com/Justagwas/TrackIndex/releases/latest/download/TrackIndexSetup.exe).
2. Launch TrackIndex.
3. Open a music folder or add a library discovered under the Windows Music folder.
4. Follow the short setup to choose whether its order should be stored in filenames, a playlist, or both.
5. Select one or several tracks and drag them into position.
6. Double-click a track or use its artwork control to begin listening.
7. Use the player to seek, adjust volume, shuffle the library, or choose a repeat mode.
8. Drop audio into the list to import it, or remove tracks you no longer want in the library.
9. Use **Ctrl+Z** and **Ctrl+Shift+Z** to undo or redo changes during the session.

## Features

- A focused sidebar for keeping multiple folder-based music libraries within reach.
- Smooth drag and drop ordering for one track or a complete selection.
- Automatic synchronization of numbered filenames and M3U or M3U8 playlists.
- Clear guidance when filenames and playlist order disagree.
- Artwork, title, artist, album, and duration display without changing embedded tags.
- A responsive local player with seeking, volume, shuffle, repeat, and source format details.
- Keyboard shortcuts and compatible Windows media controls for playback while TrackIndex is in the background.
- Support for MP3, WAV, FLAC, OGG, OPUS, M4A, AAC, WMA, AIFF, and AIF files.
- Audio import, track removal, undo, redo, and interrupted-operation recovery.
- Local operation without an account or library upload.

## Using TrackIndex

### Put every track in its place

Each TrackIndex library represents one music folder. Rearrange the songs directly in the list, use familiar multi-selection controls when several tracks belong together, and move the complete group in one action.

TrackIndex reads music files directly inside the selected folder. This keeps each library predictable and avoids quietly pulling songs from unrelated subfolders into the same order.

### Keep the order portable

Numbered filenames are useful when a device or application sorts by name. M3U and M3U8 playlists preserve an intentional sequence without changing the rest of a filename. TrackIndex can maintain either approach or keep both aligned, allowing the same collection to retain its order across more players and devices.

Existing playlist details are preserved where possible. If the folder and playlist disagree, TrackIndex asks which one should be trusted before making a change.

### Listen without leaving the library

Start a track from the list and a dedicated player appears with artwork, music details, a timeline, volume, shuffle, repeat, and familiar playback controls. It can be compacted when you want more room for the library or closed when you are finished listening.

The listening queue follows the latest successfully saved library order without interrupting the current song. Compatible keyboard and Windows media controls also provide access to playback when TrackIndex is in the background.

### Make changes with confidence

Renames, imports, removals, and playlist updates are prepared as one coordinated change. Undo and redo remain available throughout the session, and interrupted operations can be recovered safely.

Removed audio remains recoverable until the session is accepted. It is then sent to the Windows Recycle Bin rather than being permanently erased without warning.

## Project resources

- Project page: <https://www.justagwas.com/projects/trackindex>
- Download page: <https://www.justagwas.com/projects/trackindex/download>
- Releases: <https://github.com/Justagwas/TrackIndex/releases>
- SourceForge mirror: <https://sourceforge.net/projects/trackindex/>

<details>
<summary>For Developers</summary>

### Requirements

- Windows 10 or Windows 11, 64-bit.
- Python 3.14.
- Dependencies in [`TrackIndex/requirements.txt`](https://github.com/Justagwas/TrackIndex/blob/main/TrackIndex/requirements.txt).

### Running From Source

```powershell
py -3.14 -m pip install -r TrackIndex/requirements.txt
py -3.14 TrackIndex/TrackIndex.py
```

Playback requires the local `TrackIndex/native/libmpv/mpv-2.dll` runtime. The existing local runtime and its notices are retained; restore the DLL manually when setting up a fresh checkout. The Python binding is installed through `requirements.txt`; local packaging also retains the existing `TrackIndex/vendor/mpv.py` binding.

### Release Packaging

Local packaging follows the same layout and publishing workflow as the other Justagwas applications:

- `TrackIndex/ronefile.spec` builds the standalone executable.
- `TrackIndex/ronedir.spec` builds the installer payload.
- `TrackIndex/dist/ONEDIR.iss` produces the installer using the existing TrackIndex assets and information files.
- `publish-trackindex.cmd` prompts for release metadata, builds and signs both executables and the installer, and writes matching `latest.json` files to `release/` and the Astro project directory.

The publisher uses the existing Python installation, with TrackIndex's requirements and PyInstaller already installed. It does not create an environment or run a test suite. Select Python 3.14 as the default Python launcher installation. Production packaging requires Inno Setup and the configured signing certificate.

Publishing scripts, specifications, version resources, installer assets, and release outputs are local ignored files, matching the sibling repositories. A fresh checkout needs these local packaging files restored before publishing. Both packaged distributions include the playback runtime; end users do not need Python or a separate libmpv installation.

</details>

## Security and OS Warnings

- Windows SmartScreen may display a warning for a new or unsigned release.
- Obtain TrackIndex only from the official distribution locations:
  - <https://github.com/Justagwas/TrackIndex/releases>
  - <https://www.justagwas.com/projects/trackindex/download>
  - <https://sourceforge.net/projects/trackindex/>
- Refer to the repository [Security Policy](https://github.com/Justagwas/TrackIndex/security/policy) for private vulnerability reporting.

## Contributing

Contributions are welcome.

- Start with the shared [Contributing Guide](https://github.com/Justagwas/.github/blob/main/CONTRIBUTING.md)
- Follow the shared [Code of Conduct](https://github.com/Justagwas/.github/blob/main/CODE_OF_CONDUCT.md)
- Use [Issues](https://github.com/Justagwas/TrackIndex/issues) for defect reports, feature proposals, and questions

## Contact

- Email: [email@justagwas.com](mailto:email@justagwas.com)
- Website: <https://www.justagwas.com/projects/trackindex>

## License

Licensed under the GNU General Public License v3.0 (GPL-3.0).

See [`LICENSE`](https://github.com/Justagwas/TrackIndex/blob/main/LICENSE).
