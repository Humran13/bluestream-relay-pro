# Playlists

Playlists are a Pro feature for sequential, looping channel playback of
multiple local media files.

Example playlist contents:

```text
video01.mp4
video02.mp4
video03.mp4
video04.mp4
```

The server plays the files in order, then loops back to the first file and
continues 24/7. Playlists are supervised by systemd like relays and expose a
stable public HLS URL:

```text
https://example.com/hls/playlist/<name>/index.m3u8
```

## Media must be imported first

Playlist entries reference **managed media** in `/var/lib/bluestream/media`.
Files are imported through the manager so permissions are handled safely:

```
sudo bluestream-manager
Media Management > Import Media
```

Non-interactive:

```bash
sudo bluestream-manager media import /path/to/video.mp4
sudo bluestream-manager media import "https://example.com/videos/program.mp4" --name program.mp4
```

## Creating and filling a playlist

```bash
sudo bluestream-manager playlist create channel1
sudo bluestream-manager playlist add channel1 video01.mp4 video02.mp4 video03.mp4
```

Interactive options are available under **Playlist Management**.

### Creating a playlist from the web console (GUI-1D.1)

The Playlists page in the web console offers a no-command-line builder:

1. Open **Playlists** and click **+ Create Playlist**.
2. Enter a normal friendly name (e.g. `Evening Promo Loop`). The console
   normalizes it to the safe internal ID (`evening-promo-loop`) before it
   crosses the privileged boundary.
3. Tick **two or more** media files from the Media Library and give each a
   unique play order number starting at 1. Files play in that order and loop.
4. Click **Create Playlist**. The playlist is created **STOPPED** — press
   **Start** to begin playing.

The playlist HLS URL is shown in the Playlists table:

```text
https://<your-domain>/hls/playlist/<id>/index.m3u8
```

A playlist must contain **at least two** media files, may contain **at most
64** (`BLUESTREAM_PLAYLIST_MAX_ITEMS`), and each file may appear only once.
Selected files are validated against the managed-media name rule on every
layer; arbitrary paths, traversal markers and Windows-style separators are
always rejected.

## Playlist operations

| Operation | Command |
| --- | --- |
| Create | `playlist create <name>` |
| List | `playlist list` |
| Add media | `playlist add <name> <file>...` |
| Remove media | `playlist remove <name> <file>...` |
| Reorder | `playlist reorder <name> <file1> <file2> ...` (full new order) |
| Show contents | `playlist show <name>` |
| Compatibility check | `playlist check <name>` |
| Start | `playlist start <name>` |
| Stop | `playlist stop <name>` |
| Restart | `playlist restart <name>` |
| Enable at boot | `playlist enable <name>` |
| Disable at boot | `playlist disable <name>` |
| M3U8 URL | `playlist url <name>` |
| Player URL | `playlist player <name>` |
| Health | `playlist health <name>` |
| Logs | `playlist logs <name>` |

## Compatibility and automatic normalization

Any ordinary uploadable media can go into a playlist: 1080p, 720p,
low-resolution clips, different H.264 profiles, different frame rates,
different MP4 time bases, AAC-LC or other FFmpeg-decodable audio, even
video-only files. You do **not** need to re-encode files yourself.

Before a playlist starts, the root runtime wrapper normalizes every entry
into a managed cache (`/var/lib/bluestream/playlist-cache/`) using ONE fixed
baseline:

- Video: H.264 (Main), `yuv420p`, 1280x720 with aspect-ratio-preserving
  scale + pad (never stretched), fixed 25 fps, deterministic 2s keyframes,
  MP4 video time base 1/90000.
- Audio: AAC-LC 128 kbps, 44100 Hz, stereo. Video-only files automatically
  receive a silent stereo track so the playlist keeps one continuous audio
  stream.

Only those uniform "playlist-ready" artifacts are handed to FFmpeg's concat
demuxer, so heterogeneous media plays as one stable continuous loop with
stream copy. Original media files are never modified.

## Caching

Normalized artifacts are content-addressed: the cache key is the SHA-256 of
the managed-media file, never the filename.

- An unchanged file reuses its cached artifact — no re-encode on every start.
- A changed file hashes to a new key and produces a fresh artifact, so stale
  cached content is never served.
- Publication is atomic (temp file then rename); failed or incomplete
  conversions are deleted and never appear as usable artifacts.
- The cache is bounded (one artifact per distinct media content) and pruned
  automatically of artifacts that no longer match any managed media file.
- If any entry cannot be decoded/prepared, the playlist fails closed — it
  never starts, never claims HEALTHY, and the original media is untouched.

The cache directory `/var/lib/bluestream/playlist-cache/` is owned by
`root:bluestream-relay` mode `0750`; artifacts are `root:bluestream-relay`
`0640`, readable by the dropped-privilege FFmpeg process and by no one else.

## Technical notes

- Playlists are played through FFmpeg's concat demuxer with `-stream_loop -1`
  and `-re` (real-time rate); the whole ordered playlist loops
  (1 → 2 → ... → N → 1 → ...).
- The concat file is generated at runtime in `/var/lib/bluestream/run/`
  (readable by `bluestream-relay`) and lists only the prepared baseline
  artifacts.
- Playlist configuration is root-only (`/etc/bluestream/playlists/`, mode
  0600).
- **Removing an entry from a playlist never deletes the media file.** Use
  Media Management > Remove Managed Media for that, with explicit
  confirmation.
