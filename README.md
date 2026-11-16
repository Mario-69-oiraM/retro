# Retro Board

An anonymous retro board for the whole team. It's one Python file with nothing to install.

## Running a retro

1. **Facilitator:** double-click **Start Retro (Mac).command** or **Start Retro (Windows).bat**.
   You can also run `python3 retro.py` yourself.
   Leave the window open. It shows:
   - the **link to share** (for example `http://192.168.1.20:8080`)
   - the **facilitator passcode** (always `12345`)
2. **Everyone else:** open the link in any browser. They don't need Python or any other software.
3. Add cards to the columns (by default: What went well, What didn't go well, Actions and Kudos). They stay **hidden** until the facilitator reveals them.
   Each person can see their own cards while they write.
4. Facilitator: click **Facilitator**, enter the passcode, then **Reveal cards**. Voting opens, and every column
   is sorted by votes automatically, most-voted first.
5. At the end: **Export → Markdown / CSV / JSON**, then **Clear board** for next time.
   Clearing also saves an archive copy, `retro-archive-<date>.json`.

## Facilitator controls
- Reveal or hide cards
- **Hide my-vote highlights**: vote counts stay visible, but nobody's screen highlights the cards they
  voted for (handy when screen-sharing). Click **Show my-vote highlights** to turn it back on.
- Rename the board or any column (use the pencil icons)
- Add a column (**+ Add column**, up to 8) or remove one (trash icon on the column; this also deletes its cards)
- Delete any card
- **Combine cards**: once cards are revealed, drag one card onto another (on a computer, not a phone).
  They show as one card with the votes added together.
- **Split cards**: click the split icon on a combined card to put every original card back, each with its
  own text and votes, in the column it came from.
- Export (Markdown, CSV for Excel, JSON)
- Clear the board (archived automatically)

## Anonymity
- Nothing records who posted a card: no names, IP addresses, timestamps or request logs.
- To let you edit or delete your own cards, your browser keeps a private key for each card you post.
  The server stores only a hash of that key.
- Votes are stored as hashes per card, so one person's votes can't be linked across cards.

## Options
```
python3 retro.py --port 9000            # if 8080 is already in use
python3 retro.py --data other-team.json # separate board per team
```

## Notes
- Only **one** person should run the server for a given data file at a time.
- The first time it runs, your OS firewall may ask whether to allow incoming connections. Allow them,
  or teammates won't be able to reach the board.
- Everyone needs to be on the same network or VPN as the facilitator's machine.
- Data is saved to `retro-data.json` after every change. Stopping and restarting the server keeps the board.

## Run it on a public server (Render)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Mario-69-oiraM/retro)

1. Click the button above (or in Render: **New → Blueprint** and pick this repo). It uses `render.yaml`.
2. When asked for `RETRO_PASSCODE`, enter a private facilitator passcode. It's stored on Render, not in this repo.
3. Render gives you a public `https://retro-….onrender.com` link to share.

Settings the server reads:

| Variable | Purpose | Default |
|---|---|---|
| `PORT` | Port to listen on (Render sets this) | `8080` |
| `RETRO_PASSCODE` | Facilitator passcode | `12345` |
| `RETRO_DATA` | Path of the board data file | `retro-data.json` next to `retro.py` |

On Render's free plan the service sleeps after about 15 minutes idle and the board data resets when it restarts. To keep data, switch to a paid plan and enable the disk in `render.yaml`.
