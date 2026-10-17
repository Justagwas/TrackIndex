# TrackIndex

<div align="center">

[![Code: GitHub](https://img.shields.io/badge/Code-GitHub-111827.svg?style=flat&logo=github&logoColor=white)](https://github.com/Justagwas/TrackIndex)
[![Website](https://img.shields.io/badge/Website-TrackIndex-0ea5e9.svg?style=flat&logo=google-chrome&logoColor=white)](https://www.justagwas.com/projects/trackindex/)
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

<p align="center">Arrange music by dragging tracks, and keep the order in filenames, playlists, or both.</p>

<div align="center">

[![Version](https://img.shields.io/github/v/tag/Justagwas/TrackIndex.svg?label=Version)](https://github.com/Justagwas/TrackIndex/tags)
[![License](https://img.shields.io/github/license/Justagwas/TrackIndex.svg)](https://github.com/Justagwas/TrackIndex/blob/main/LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/Justagwas/TrackIndex/main.svg?style=flat&cacheSeconds=3600)](https://github.com/Justagwas/TrackIndex/commits/main)
[![Open Issues](https://img.shields.io/github/issues/Justagwas/TrackIndex.svg)](https://github.com/Justagwas/TrackIndex/issues)
[![Stars](https://img.shields.io/github/stars/Justagwas/TrackIndex.svg?style=flat&cacheSeconds=3600)](https://github.com/Justagwas/TrackIndex/stargazers)
![Downloads (7d)](https://img.shields.io/badge/dynamic/json?style=flat&url=https%3A%2F%2Fdownload-stats-worker.justagwas.workers.dev%2Fdownloads%2Ftrackindex%3Frange%3Dweek&query=%24.data.weekly.all&label=Downloads%20(7d))

</div>

## Overview

Open a music folder or create a playlist, then drag your tracks into the order you want. Listen as you organize, and undo or redo library changes while TrackIndex is open.

Want that order to appear in File Explorer too? TrackIndex can add numbers to your filenames so the tracks appear in your chosen sequence when sorted by **Name**. You can also save the order in a playlist file for music players that support it, without renaming your tracks.

## Features

- **Music folders and playlists:** Several local libraries within reach, with playlist creation and support for common audio formats.
- **Drag-and-drop ordering:** Individual tracks or whole selections moved together.
- **Order beyond the app:** Optional numbered filenames and M3U/M3U8 playlist files.
- **Built-in music player:** Artwork, track details, seeking, shuffle, repeat and a compact view.
- **Undo and redo:** Reordering, added or removed tracks, and playlist changes while TrackIndex is open.
- **Background loading:** Artwork and track details load as you browse, prioritizing the tracks in view.
- **Standalone option:** Run without installation, alongside the regular Windows installer.

## Basic usage

1. Get the [Windows installer or standalone app](https://www.justagwas.com/projects/trackindex/). Choose the installer for a normal setup, or standalone to run without installing.
2. Select **Open Folder** for existing music, or **Create Playlist** for a new folder under the **Music folder** selected in Settings.
3. Choose **Filename Prefixes** for numbered filenames, **Playlist File** to save the order without renaming tracks, or both.
4. Drop audio files into the track list to copy them into the library. The original files stay unchanged. Drag one track or a selection into position.
5. Double-click a track or use its artwork control to listen.
6. Use **Ctrl+Z** to undo and **Ctrl+Shift+Z** to redo library changes while TrackIndex is open. See [Undo, removal and recovery](#undo-removal-and-recovery) for what happens to removed files.

## Using TrackIndex

### Music folders and new playlists

Each library uses the audio files directly inside one folder. Subfolders can appear as separate libraries beneath your Music folder; their tracks are not combined into the parent library.

**Create Playlist** creates a new folder and an empty M3U8 playlist under the **Music folder** selected in Settings. Add music by dropping audio files into the track list; imports copy the files and leave the originals intact.

### Filename and playlist order

Choose **Filename Prefixes** to store order in numbered filenames, **Playlist File** to save it without renumbering audio, or both. For numbered files, sort by **Name** in File Explorer to see the saved sequence. M3U and M3U8 playlists retain playback order in compatible players.

If an existing playlist and filenames disagree, setup asks which order to use. TrackIndex keeps the selected filenames and playlist references aligned without editing embedded tags or changing audio content.

### Playback and saved order

Compact the player to leave more room for the library. Compatible keyboard and Windows media controls also work in the background.

Shuffle affects playback only, leaving the saved library order unchanged. After successfully saved library changes, the queue updates while keeping the current song where possible.

### Undo, removal and recovery

While TrackIndex is open, you can undo and redo track reordering, filename changes, added or removed tracks, and playlist updates.

Removed audio stays in local recovery storage while TrackIndex is open, so you can undo the removal. On normal close, it goes to the Windows Recycle Bin. If cleanup cannot finish, recovery data is retained for later handling. Interrupted changes are checked on the next start, with recovery options when needed.

### Supported audio formats

MP3, WAV, FLAC, OGG, OPUS, M4A, AAC, WMA, AIFF and AIF.

### Installer and standalone

The standalone executable runs without installation. Both versions store settings in `%LOCALAPPDATA%\TrackIndex`; settings do not travel alongside the executable. Library recovery data stays inside the affected music folder.

## Project resources

- Project page: <https://www.justagwas.com/projects/trackindex/>
- Download page: <https://www.justagwas.com/projects/trackindex/download/>
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
  - <https://www.justagwas.com/projects/trackindex/download/>
  - <https://sourceforge.net/projects/trackindex/>
- Refer to the repository [Security Policy](https://github.com/Justagwas/TrackIndex/security/policy) for private vulnerability reporting.

## Contributing

Contributions are welcome.

- Start with the shared [Contributing Guide](https://github.com/Justagwas/.github/blob/main/CONTRIBUTING.md)
- Follow the shared [Code of Conduct](https://github.com/Justagwas/.github/blob/main/CODE_OF_CONDUCT.md)
- Use [Issues](https://github.com/Justagwas/TrackIndex/issues) for defect reports, feature proposals, and questions

## Contact

- Email: [email@justagwas.com](mailto:email@justagwas.com)
- Website: <https://www.justagwas.com/projects/trackindex/>

## License

Licensed under the GNU General Public License v3.0 (GPL-3.0).

See [`LICENSE`](https://github.com/Justagwas/TrackIndex/blob/main/LICENSE).
