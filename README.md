# MyTV

Four 24/7 TV channels, hosted on a Mac, watched on an Apple TV.

| | Channel | Content |
|---|---|---|
| 1 | Classic Movies | Public-domain American features |
| 2 | Comedy Movies | Public-domain American comedies |
| 3 | Sitcoms | Public-domain episodes of 1950s and 60s series |
| 4 | Mystery & Thrillers | Noir, crime, detective and suspense films |

Anything you put in `library/` joins the matching channel.

## Use it

On a new Mac, either hand this repository to a coding agent and ask it to set MyTV up (it follows
[AGENTS.md](AGENTS.md)), or:

```bash
git clone https://github.com/lukejoneslj/mytv ~/MyTV && cd ~/MyTV
```
```bash
scripts/setup.sh     # once per Mac
```
```bash
scripts/start.sh     # prints the addresses to use
```
```bash
scripts/stop.sh
```
```bash
scripts/doctor.sh    # checks everything and prints the addresses
```
```bash
node scripts/test/stream_check.mjs   # proves streaming a tab works, end to end
```

Or double-click **Start MyTV.command**. Dashboard: http://localhost:8400

On the first start a channel shows a station card until its first title has downloaded, usually 5 to 15 minutes.

## Apple TV

Apple's own TV app cannot load custom playlists, so install an IPTV app from the App Store on the Apple TV, such as
**UHF**, **iPlayTV**, **IPTVX** or **GSE Smart IPTV**. Then:

1. Make sure the Apple TV and the Mac are on the same network.
2. In the app choose *Add playlist* > *M3U URL* and enter the address `start.sh` printed: `http://<mac-ip>:8400/playlist.m3u`
3. If the app asks for a guide (EPG or XMLTV) address: `http://<mac-ip>:8400/epg.xml`. Most apps find it by themselves.
4. If the app has a player choice and one does not work, try the other. For apps that only accept MPEG-TS:
   `http://<mac-ip>:8400/playlist.m3u?format=ts`

The Mac's IP can change. Either reserve it in your router, or use the Bonjour address shown under Settings > Network
(`http://your-mac.local:8400/playlist.m3u`). If macOS asks whether Python may accept incoming connections, allow it.

No IPTV app? Open the dashboard in Safari on an iPhone, iPad or Mac, press Watch, and AirPlay it.

## Your own videos

Copy files into these folders. They are picked up within five minutes, never modified, never deleted.

```
library/classic-movies/      Title (1954).mp4
library/comedy-movies/       Title (1987).mkv
library/sitcoms/Show Name/   Show Name S01E02 Episode Title.mkv
library/mystery-thrillers/
```

Any format ffmpeg reads works. The other ways to add content are a direct link to a video file (Channels page) and a live
stream as an extra pass-through channel (Settings).

## Stream anything from the Mac

Channels **11, 12 and 13** (Live 1 to 3) show whatever you send them from Chrome on the Mac: a video site, a video file,
a game, any window. Each one is its own channel on the Apple TV with its own sound, so several can run at once.

1. Play the video in its own Chrome tab. For a file on the Mac, drag it onto Chrome.
2. Open the dashboard **on this Mac** (http://localhost:8400/stream), press *Choose a tab to stream* on a channel, pick the
   tab, keep *Also share tab audio* on, press Share.
3. In that tab press the player's full-screen button: the video fills the TV and stays inside its tab on the Mac.
4. Repeat on the next channel for another tab. Leave the Stream page open; the other tabs can be in the background.

Streamed tabs are silent on the Mac so they do not play over each other; *Listen here* lets you hear one of them.
With nothing streaming, a channel shows a "nothing is streaming" card. Rename, renumber, add or remove these channels on
the Stream page. The Quality setting at the top: plain 1080p is right for films, TV and most videos and handles five or
more at once; 60 fps is for sports and games, about three at once. The TV is 10 to 15 seconds behind the Mac.
Sites that block screen sharing show a black picture; MyTV cannot change that. Safari and Firefox cannot do this; use
Chrome or Edge.

## Posters

Posters come from the source, TVmaze (shows), or a frame of the film. For real movie posters, create a free key at
https://www.themoviedb.org/settings/api and paste it under Settings > Posters and details.

## Starting automatically

- **Project on the internal disk:** `scripts/autostart.sh on` (undo with `off`).
- **Project on an external drive:** macOS blocks background services from external drives. Add *Start MyTV.command* under
  System Settings > General > Login Items instead.

While MyTV runs, the Mac will not idle-sleep. Closing the lid still sleeps it unless it is on power with an external display.

## Moving to another Mac

Clone this repository on the new Mac and set it up as above: the live channels work straight away and the film channels
fill themselves again. To keep history, settings and downloaded titles, stop MyTV on both Macs and copy the old `data`
folder into the new one. Then update the address in the Apple TV app.

## When something looks wrong

| Symptom | Look at |
|---|---|
| Anything | Downloads page > Activity, `data/logs/mytv.log`, `http://localhost:8400/health` |
| Apple TV cannot connect | Same Wi-Fi? Guest networks block devices from each other. Mac firewall? |
| Station card stays on | Downloads page: is there an error, or a storage pause? |
| A bad copy of a film | Library > the title > *Never air this* |
| A stream is black | The site blocks screen sharing |
| A stream has no sound | Stop it and share the tab again with *Also share tab audio* on |

How it works: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
