# Retro Board

An anonymous retro board for the whole team. It's one Python file with nothing to install.

## Running a retro

1. **Facilitator:** double-click **Start Retro (Mac).command** or **Start Retro (Windows).bat**.
   You can also run `python3 retro.py` yourself.
   Leave the window open. It shows:
   - the **link to share** (for example `http://192.168.1.20:8080`)
   - the **facilitator passcode** (always `12345`)
2. **Everyone else:** open the link in any browser. They don't need Python or any other software.
3. Add cards to the columns (three by default). They stay **hidden** until the facilitator reveals them.
   Each person can see their own cards while they write.
4. Facilitator: click **Facilitator**, enter the passcode, then **Reveal cards**. Voting opens.
5. At the end: **Export → Markdown / CSV / JSON**, then **Clear board** for next time.
   Clearing also saves an archive copy, `retro-archive-<date>.json`.

## Facilitator controls
- Reveal or hide cards
- Rename the board or any column (use the pencil icons)
- Add a column (**+ Add column**, up to 8) or remove one (trash icon on the column; this also deletes its cards)
- Delete any card
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
