# Email export

Finds email chains in your Proton Mail mailbox and saves each one as a PDF,
with an index page for browsing them. A chain is exported when any email in it:

- contains one of your keywords in its subject or body, or
- was sent by one of the people you list (or, if you choose, was sent to them
  or copied to them), or
- was sent within one of your date ranges.

The dates can instead be used as a limit: only chains that match a keyword or
a person and also have an email inside the dates.

Nothing in your mailbox is changed. The script only reads.

## Before you start

- **Proton Mail Bridge** is open, signed in, and has finished syncing.
- **Google Chrome** is installed (Edge or Brave also work). It is used
  invisibly to make the PDFs.

## 1. Install and run it

1. Open **Terminal** (press Cmd+Space, type `Terminal`, press Return).
2. Paste this line and press Return:

   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/markgbrisbane/proton-clerk/main/install.sh)"
   ```

Everything is put in a folder called `mail-export` in your home folder. To
open it in Finder, choose Go > Go to Folder and type `~/mail-export`.

## 2. Choose what to look for

The settings file opens in TextEdit. Fill in your keywords, senders and dates
following the examples in the file. Save with Cmd+S, then quit TextEdit with
Cmd+Q. The export starts when TextEdit quits.

Two settings change how the rules behave:

- `look_for_people_in: [from, to, cc]` also finds chains where a listed person
  only received an email or was cc'd. Left as `[from]`, only emails they sent
  count. The index and the PDFs show `sender:` or `recipient:` next to each
  match, so you can see which it was. Bcc can't be searched.
- `limit_to_date_ranges: true` turns the dates into a limit. A chain is then
  exported only if it matches a keyword or a person and has at least one email
  inside the dates. The whole chain is exported, so a PDF can include emails
  from outside the dates. Left as `false`, the dates are a rule of their own.

The first time:

- It downloads a small helper into the folder. This needs an internet connection.
- It asks for your **Bridge username and password**. Find them in the Bridge
  app: click your account, then look under "Mailbox details". The Bridge
  password is different from your Proton password.
- It downloads a copy of your mail into the `cache` folder. For a large
  mailbox this can take a long time. You can stop it with Ctrl+C and run it
  again later; it carries on where it left off. Later runs only fetch new mail.

## 3. Look at the results

Each run creates a new dated folder inside `exports`, and opens its index in
your browser:

| Item | What it is |
| --- | --- |
| `index.html` | Opens in your browser. Type in the box to filter, click a column heading to sort, click a subject to see each email in the chain. |
| `index.csv` | The same list, for Excel or Numbers. |
| `index.json` | The same data in full, for other software. |
| `pdf/` | One PDF per chain. Names start with the date and time of the chain's first email, so sorting by name puts them in date order. |
| `attachments/` | Attached files, in a folder named after the chain. |
| `originals/` | The original email files (`.eml`), in a folder named after the chain. |

## Running it again

Double-click **Run Mail Export.command** in the `mail-export` folder. The
settings open in TextEdit again; change them or just quit TextEdit, and the
export runs.

Pasting the line from step 1 again updates the script. Your settings, Bridge
login, downloaded mail and earlier exports are kept.

## Trying out different rules

For anyone comfortable in Terminal, these save time after changing
`config.yaml`:

- `sh ~/mail-export/run.sh --dry-run` lists the chains that match without
  making any PDFs.
- `sh ~/mail-export/run.sh --offline` skips Bridge and searches the mail
  already downloaded.

## If something goes wrong

The script stops with a message starting `STOPPED:` that says what to fix.
Everything it printed is also saved in `last-run.log` in the `mail-export`
folder, which is the file to send when asking for help. The usual causes:

- **Couldn't reach Proton Mail Bridge**: the Bridge app isn't open, or the
  port in Bridge's settings isn't 1143. Change `imap_port` in `config.yaml`
  to match.
- **Bridge rejected the username or password**: delete the file `bridge.env`
  and run again to re-enter them.
- **config.yaml couldn't be read**: usually indentation, or a value starting
  with `@` that needs quotes, like `"@example.com"`.

## Privacy

- `cache` and `exports` hold unencrypted copies of your email. Delete both
  folders when you have finished.
- `last-run.log` lists the subject lines of matching emails.
- `bridge.env` holds your Bridge password. It only works on this Mac while
  Bridge is running, but don't share the file. It contains two lines:

  ```
  BRIDGE_USERNAME=you@proton.me
  BRIDGE_PASSWORD=the-password-from-bridge
  ```
